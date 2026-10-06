from datetime import datetime, timezone

import pytest

from pisentinel.demo import build_demo_database


def test_demo_builder_creates_synthetic_timeline(tmp_path):
    path = tmp_path / "demo.db"
    database = build_demo_database(path, now=datetime(2026, 10, 6, 12, tzinfo=timezone.utc))
    devices = database.list_devices()
    incidents = database.list_incidents(now="2026-10-06T12:00:00+00:00")
    assert len(devices) == 7
    assert len(incidents) == 2
    assert {incident["ongoing"] for incident in incidents} == {True, False}
    assert all(device["mac"].startswith("02:00:00:") for device in devices)


def test_demo_builder_refuses_to_replace_database_without_flag(tmp_path):
    path = tmp_path / "demo.db"
    build_demo_database(path)
    with pytest.raises(FileExistsError):
        build_demo_database(path)
