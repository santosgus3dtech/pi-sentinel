from fastapi.testclient import TestClient

from pisentinel.api import create_app
from pisentinel.config import Settings
from pisentinel.db import Database
from pisentinel.printers.creality_k1c import CameraStream


def test_read_only_printer_endpoints_use_mock_adapter(tmp_path):
    settings = Settings(db_path=tmp_path / "api.db", mock_printer_enabled=True)
    client = TestClient(create_app(settings))
    printers = client.get("/api/printers")
    assert printers.status_code == 200
    assert printers.json()[0]["id"] == 0
    assert printers.json()[0]["state"] == "PRINTING"
    detail = client.get("/api/printers/0")
    assert detail.status_code == 200 and detail.json()["current_file"] == "peca-demonstracao.3mf"
    status = client.get("/api/printers/0/status")
    assert status.status_code == 200 and status.json()["temperatures"]["bed"]["target"] == 60.0
    capabilities = client.get("/api/printers/0/capabilities")
    assert capabilities.json()["capabilities"]["controls"] is False
    assert client.post("/api/printers", json={}).status_code == 405


def test_printer_endpoints_return_linked_device_and_404(tmp_path):
    settings = Settings(db_path=tmp_path / "linked.db")
    database = Database(settings.db_path)
    device_id = database.upsert_device("192.168.10.155", "fc:ee:28:00:00:01", "K1C")
    printer_id = database.create_printer("K1C de teste", "Creality", "K1C", "mock",
                                         device_id=device_id, adapter_config={"state": "IDLE"})
    client = TestClient(create_app(settings, database))
    detail = client.get(f"/api/printers/{printer_id}")
    assert detail.status_code == 200
    assert detail.json()["ip"] == "192.168.10.155"
    assert detail.json()["device_id"] == device_id
    assert client.get("/api/printers/999").status_code == 404


def test_health_and_camera_endpoints_remain_read_only(tmp_path):
    class StubPrinterService:
        def get_health(self, printer_id):
            if printer_id != 7:
                raise KeyError(printer_id)
            return {"id": 7, "online": True, "connection_status": "healthy",
                    "last_seen": "2026-09-24T20:26:20Z", "latency_ms": 7.4,
                    "health": {"overall": "healthy", "components": {}}}

        def open_camera(self, printer_id):
            if printer_id != 7:
                raise KeyError(printer_id)
            return CameraStream("multipart/x-mixed-replace; boundary=frame", iter([b"--frame\r\n"]))

    settings = Settings(db_path=tmp_path / "stream.db")
    database = Database(settings.db_path)
    client = TestClient(create_app(settings, database, StubPrinterService()))

    health = client.get("/api/printers/7/health")
    assert health.status_code == 200 and health.json()["latency_ms"] == 7.4
    camera = client.get("/api/printers/7/camera")
    assert camera.status_code == 200
    assert camera.headers["content-type"] == "multipart/x-mixed-replace; boundary=frame"
    assert camera.content == b"--frame\r\n"
    assert client.post("/api/printers/7/camera", json={}).status_code == 405


def test_printer_file_details_history_and_guarded_start_endpoints(tmp_path):
    class StubPrinterService:
        def get_details(self, printer_id):
            assert printer_id == 8
            return {"id": 8, "details": {"source": "fixture", "parameters": {"state": 0}}}

        def list_files(self, printer_id):
            assert printer_id == 8
            return {"id": 8, "files": [{"path": "part.gcode", "print_ready": True}], "count": 1}

        def inspect_file(self, printer_id, path):
            assert (printer_id, path) == (8, "part.gcode")
            return {"id": 8, "file": {"path": path}, "plates": []}

        def list_history(self, printer_id):
            assert printer_id == 8
            return {"id": 8, "history": [{"id": 1, "completed": True}], "count": 1}

        def start_print(self, printer_id, path, options):
            assert (printer_id, path) == (8, "part.gcode")
            assert options["bed_leveling"] is True
            return {"id": 8, "accepted": True, "message": "confirmado"}

    settings = Settings(db_path=tmp_path / "operations.db", printer_controls_enabled=True)
    client = TestClient(create_app(settings, Database(settings.db_path), StubPrinterService()))

    assert client.get("/api/printers/8/details").json()["details"]["source"] == "fixture"
    assert client.get("/api/printers/8/files").json()["count"] == 1
    assert client.get("/api/printers/8/files/inspect", params={"path": "part.gcode"}).status_code == 200
    assert client.get("/api/printers/8/history").json()["count"] == 1
    payload = {"path": "part.gcode", "confirmation": "IMPRIMIR"}
    assert client.post("/api/printers/8/print", json=payload).status_code == 403
    assert client.post(
        "/api/printers/8/print", json=payload, headers={"Origin": "http://evil.example"}
    ).status_code == 403
    response = client.post(
        "/api/printers/8/print", json=payload, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 202
    assert response.json()["accepted"] is True
