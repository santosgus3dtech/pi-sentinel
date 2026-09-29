import json
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from pisentinel.api import create_app
from pisentinel.config import Settings
from pisentinel.db import Database, utc_timestamp
from pisentinel.speedtest import OoklaSpeedtestRunner, SpeedtestError, parse_ookla_json
from pisentinel.speedtest_worker import SpeedtestWorker


BASE = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


def stamp(minutes=0):
    return utc_timestamp(BASE + timedelta(minutes=minutes))


def ookla_payload(**overrides):
    data = {
        "type": "result",
        "ping": {"latency": 8.4, "jitter": 1.2},
        "download": {"bandwidth": 12_500_000, "bytes": 125_000_000},
        "upload": {"bandwidth": 2_500_000, "bytes": 25_000_000},
        "packetLoss": 0,
        "isp": "Example ISP",
        "interface": {"externalIp": "203.0.113.1", "internalIp": "192.168.10.244", "macAddr": "secret"},
        "server": {"id": 1234, "name": "Lisboa", "location": "Lisboa", "country": "Portugal"},
        "result": {"url": "https://example.invalid/private-result"},
    }
    data.update(overrides)
    return json.dumps(data)


def measurement(success=True, minute=0, download=100.0, upload=20.0, error=None):
    return {
        "success": success, "started_at": stamp(minute), "completed_at": stamp(minute + 1),
        "duration_seconds": 60.0, "download_mbps": download if success else None,
        "upload_mbps": upload if success else None, "ping_ms": 8.4 if success else None,
        "jitter_ms": 1.2 if success else None, "packet_loss_pct": 0 if success else None,
        "download_bytes": 125_000_000 if success else None, "upload_bytes": 25_000_000 if success else None,
        "total_bytes": 150_000_000 if success else None, "server_id": "1234" if success else None,
        "server_name": "Lisboa" if success else None, "server_location": "Lisboa" if success else None,
        "server_country": "Portugal" if success else None, "isp": "Example ISP" if success else None,
        "error": error,
    }


def complete(database, data, trigger="manual", requested_minute=0):
    job = database.request_speedtest(trigger, stamp(requested_minute))
    claimed = database.claim_speedtest_job(stamp(requested_minute))
    assert claimed and claimed["id"] == job["id"]
    return database.complete_speedtest_job(job["id"], data)


def test_ookla_json_is_normalized_and_discards_client_identity():
    result = parse_ookla_json(ookla_payload())
    assert result["download_mbps"] == 100
    assert result["upload_mbps"] == 20
    assert result["total_bytes"] == 150_000_000
    assert result["server_id"] == "1234"
    serialized = json.dumps(result)
    assert "203.0.113.1" not in serialized
    assert "192.168.10.244" not in serialized
    assert "private-result" not in serialized
    assert "secret" not in serialized


def test_ookla_json_requires_real_bandwidth_fields():
    with pytest.raises(SpeedtestError, match="download e upload"):
        parse_ookla_json(ookla_payload(download={"bytes": 10}))


def test_runner_uses_argument_list_and_retries_once():
    calls = []
    responses = iter([
        subprocess.CompletedProcess([], 1, "", "temporary failure"),
        subprocess.CompletedProcess([], 0, ookla_payload(), ""),
    ])
    moments = iter([BASE, BASE + timedelta(seconds=12)])

    def command(args, **kwargs):
        calls.append((args, kwargs))
        return next(responses)

    runner = OoklaSpeedtestRunner("/usr/bin/speedtest", 180, command_runner=command,
                                  sleeper=lambda _: None, clock=lambda: next(moments))
    result = runner.run()
    assert result["success"] is True and result["duration_seconds"] == 12
    assert len(calls) == 2
    assert calls[0][0] == ["/usr/bin/speedtest", "--accept-license", "--accept-gdpr", "--format=json"]
    assert calls[0][1].get("shell") is not True


def test_missing_ookla_is_a_stored_failure_without_retry():
    command = Mock(side_effect=FileNotFoundError())
    moments = iter([BASE, BASE + timedelta(seconds=1)])
    result = OoklaSpeedtestRunner(command_runner=command, sleeper=lambda _: None,
                                  clock=lambda: next(moments)).run()
    assert result["success"] is False
    assert "não encontrado" in result["error"]
    assert command.call_count == 1


def test_schedule_waits_one_interval_and_queue_prevents_overlap(tmp_path):
    database = Database(tmp_path / "speed.db")
    assert database.schedule_speedtest_if_due(stamp(0)) is None
    config = database.get_speedtest_config()
    assert config["next_run_at"] == stamp(360)
    assert database.schedule_speedtest_if_due(stamp(359)) is None
    job = database.schedule_speedtest_if_due(stamp(360))
    assert job and job["trigger"] == "scheduled"
    duplicate = database.request_speedtest("manual", stamp(361))
    assert duplicate["id"] == job["id"] and duplicate["already_pending"] is True


def test_configured_low_speed_opens_and_recovers_incident(tmp_path):
    database = Database(tmp_path / "incidents.db")
    database.update_speedtest_config({"download_min_mbps": 80.0, "failure_threshold": 2,
                                      "recovery_threshold": 1}, stamp(0))
    complete(database, measurement(minute=1, download=50), requested_minute=1)
    assert database.speedtest_dashboard(7, stamp(3))["incidents"] == []
    complete(database, measurement(minute=3, download=60), requested_minute=3)
    incident = database.speedtest_dashboard(7, stamp(5))["incidents"][0]
    assert incident["metric"] == "download" and incident["ongoing"] is True
    assert incident["started_at"] == stamp(2)
    complete(database, measurement(minute=5, download=90), requested_minute=5)
    incident = database.speedtest_dashboard(7, stamp(7))["incidents"][0]
    assert incident["ongoing"] is False and incident["recovered_at"] == stamp(6)


def test_failed_speedtests_create_availability_incident_but_not_fake_low_speed(tmp_path):
    database = Database(tmp_path / "availability.db")
    database.update_speedtest_config({"download_min_mbps": 80.0, "failure_threshold": 2}, stamp(0))
    complete(database, measurement(False, 1, error="offline"), requested_minute=1)
    complete(database, measurement(False, 3, error="offline"), requested_minute=3)
    incidents = database.speedtest_dashboard(7, stamp(5))["incidents"]
    assert [item["metric"] for item in incidents] == ["availability"]


def test_speedtest_api_queues_and_updates_configuration(tmp_path):
    settings = Settings(db_path=tmp_path / "api.db", cidr="192.168.10.0/24")
    database = Database(settings.db_path)
    client = TestClient(create_app(settings, database))
    overview = client.get("/api/speedtests?days=7")
    assert overview.status_code == 200 and overview.json()["provider"] == "Ookla"
    queued = client.post("/api/speedtests/run", json={})
    assert queued.status_code == 202 and queued.json()["status"] == "queued"
    rejected = client.patch("/api/speedtests/config", json={"interval_minutes": 30})
    assert rejected.status_code == 422
    updated = client.patch("/api/speedtests/config", json={"interval_minutes": 720,
                                                            "download_min_mbps": 100})
    assert updated.status_code == 200
    assert updated.json()["interval_minutes"] == 720
    assert updated.json()["download_min_mbps"] == 100


def test_worker_processes_one_job_without_touching_collector(tmp_path):
    settings = Settings(db_path=tmp_path / "worker.db", speedtest_poll_seconds=5)
    database = Database(settings.db_path)
    database.request_speedtest("manual", stamp(0))
    runner = Mock()
    runner.run.return_value = measurement(minute=0)
    worker = SpeedtestWorker(settings, runner)
    assert worker.cycle() is True
    dashboard = database.speedtest_dashboard(7, stamp(2))
    assert dashboard["active_job"] is None
    assert dashboard["last_result"]["download_mbps"] == 100
    runner.run.assert_called_once_with()
