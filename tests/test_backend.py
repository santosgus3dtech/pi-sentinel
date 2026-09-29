from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from pisentinel.api import create_app
from pisentinel.config import Settings
from pisentinel.db import Database, utc_timestamp


def stamp(minutes: int) -> str:
    return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)).isoformat()


def make_client(tmp_path, **overrides):
    settings = Settings(db_path=tmp_path / 'network.db', label='Lab-Network',
                        cidr='192.168.10.0/24', interface='wlan0',
                        ssids=('Lab-Network', 'Lab-Network-5G'), **overrides)
    database = Database(settings.db_path, settings.failure_threshold,
                        settings.recovery_threshold, settings.retention_days,
                        settings.incident_retention_days)
    return TestClient(create_app(settings, database)), database


def test_device_outage_and_confirmed_recovery_create_one_incident(tmp_path):
    database = Database(tmp_path / 'state.db', failure_threshold=3, recovery_threshold=2)
    device = database.upsert_device('192.168.10.20', 'aa:bb:cc:dd:ee:01', 'printer', stamp(0))
    for minute in (1, 2):
        assert database.record_probe('device', device, False, now=stamp(minute))['status'] == 'online'
    result = database.record_probe('device', device, False, now=stamp(3))
    assert result['status'] == 'offline' and result['incident_id']
    assert database.record_probe('device', device, True, 2.4, stamp(4))['status'] == 'offline'
    assert database.record_probe('device', device, True, 2.1, stamp(5))['status'] == 'online'
    incidents = database.list_incidents(now=stamp(6))
    assert len(incidents) == 1
    assert incidents[0]['started_at'] == utc_timestamp(stamp(1))
    assert incidents[0]['recovered_at'] == utc_timestamp(stamp(5))
    assert incidents[0]['duration_seconds'] == 240
    assert incidents[0]['ongoing'] is False


def test_ip_reassignment_keeps_annotations_with_old_mac(tmp_path):
    database = Database(tmp_path / 'identity.db')
    old_id = database.upsert_device('192.168.10.40', 'aa:bb:cc:00:00:01', now=stamp(0))
    database.update_device(old_id, {'name': 'Impressora 3D', 'notes': 'Mesa principal', 'group': 'impressoras_3d'})
    new_id = database.upsert_device('192.168.10.40', 'aa:bb:cc:00:00:02', now=stamp(1))
    assert new_id != old_id
    devices = {item['id']: item for item in database.list_devices()}
    assert devices[old_id]['ip'] is None
    assert devices[old_id]['name'] == 'Impressora 3D'
    assert devices[old_id]['group'] == 'impressoras_3d'
    assert devices[new_id]['ip'] == '192.168.10.40'


def test_stale_collector_never_reports_cached_device_as_online(tmp_path):
    client, database = make_client(tmp_path, stale_after_seconds=10)
    database.upsert_device('192.168.10.8', 'aa:bb:cc:00:00:08', now=stamp(0))
    database.set_meta('collector_last_seen', stamp(0))
    response = client.get('/api/summary')
    assert response.status_code == 200
    summary = response.json()
    assert summary['collector']['stale'] is True
    assert summary['counts'] == {'devices': 1, 'online': 0, 'offline': 0, 'unknown': 1, 'open_incidents': 0}
    assert client.get('/api/devices').json()[0]['status'] == 'unknown'


def test_device_edit_and_target_workflow_require_json_and_same_origin(tmp_path):
    client, database = make_client(tmp_path)
    device_id = database.upsert_device('192.168.10.9', 'aa:bb:cc:00:00:09', now=stamp(0))
    assert client.patch(f'/api/devices/{device_id}', content='name=x').status_code == 415
    assert client.patch(f'/api/devices/{device_id}', json={'name': 'PC', 'group': 'computadores'},
                        headers={'Origin': 'https://evil.example'}).status_code == 403
    edited = client.patch(f'/api/devices/{device_id}', json={'name': 'PC', 'notes': 'Escritório', 'group': 'computadores'})
    assert edited.status_code == 200
    assert edited.json()['name'] == 'PC'
    cellular = client.patch(f'/api/devices/{device_id}', json={'group': 'celular'})
    assert cellular.status_code == 200 and cellular.json()['group'] == 'celular'
    created = client.post('/api/targets', json={'name': 'Roteador', 'host': '192.168.10.1',
                                                 'scope': 'local', 'kind': 'icmp', 'enabled': True})
    assert created.status_code == 201
    target_id = created.json()['id']
    assert client.patch(f'/api/targets/{target_id}', json={'host': 'https://bad.example'}).status_code == 422
    assert client.request('DELETE', f'/api/targets/{target_id}', json={}).status_code == 204


def test_discovery_is_queued_without_running_a_command(tmp_path):
    client, database = make_client(tmp_path)
    first = client.post('/api/discovery', json={})
    second = client.post('/api/discovery', json={})
    assert first.status_code == second.status_code == 202
    assert first.json()['id'] == second.json()['id']
    assert database.consume_discovery_request(now=stamp(1))['status'] == 'consumed'


def test_retention_removes_old_samples_and_closed_incidents(tmp_path):
    database = Database(tmp_path / 'retention.db', failure_threshold=1, recovery_threshold=1,
                        retention_days=1, incident_retention_days=2)
    target = database.create_target('Internet', '1.1.1.1', now=stamp(0))
    database.record_probe('target', target, True, 5, stamp(1))
    database.record_probe('target', target, False, now=stamp(2))
    database.record_probe('target', target, True, 6, stamp(3))
    removed = database.prune(now=(datetime(2026, 1, 4, tzinfo=timezone.utc)).isoformat())
    assert removed['samples'] == 3
    assert removed['incidents'] == 1
