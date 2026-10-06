from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import Database


DEVICES = (
    ("192.168.50.1", "02:00:00:00:00:01", "gateway.demo", "Demo gateway", "infraestrutura"),
    ("192.168.50.20", "02:00:00:00:00:20", "workstation.demo", "Design workstation", "computadores"),
    ("192.168.50.30", "02:00:00:00:00:30", "storage.demo", "Studio storage", "infraestrutura"),
    ("192.168.50.41", "02:00:00:00:00:41", "printer-a.demo", "Printer A", "impressoras_3d"),
    ("192.168.50.42", "02:00:00:00:00:42", "printer-b.demo", "Printer B", "impressoras_3d"),
    ("192.168.50.55", "02:00:00:00:00:55", "camera.demo", "Workshop camera", "infraestrutura"),
    ("192.168.50.70", "02:00:00:00:00:70", "phone.demo", "Demo phone", "celular"),
)


def timestamp(base: datetime, minutes: int) -> str:
    return (base + timedelta(minutes=minutes)).isoformat()


def build_demo_database(path: Path, *, replace: bool = False, now: datetime | None = None) -> Database:
    path = path.resolve()
    if path.exists():
        if not replace:
            raise FileExistsError(f"Refusing to replace existing database: {path}")
        path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)

    base = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(hours=3)
    database = Database(path, failure_threshold=2, recovery_threshold=1)
    ids: dict[str, int] = {}
    for index, (ip, mac, hostname, name, group) in enumerate(DEVICES):
        device_id = database.upsert_device(ip, mac, hostname, timestamp(base, index))
        database.update_device(device_id, {"name": name, "notes": "Synthetic portfolio fixture", "group": group})
        database.record_probe("device", device_id, True, 1.8 + index, timestamp(base, 10 + index))
        ids[name] = device_id

    # One recovered incident and one ongoing outage make the timeline useful immediately.
    storage = ids["Studio storage"]
    database.record_probe("device", storage, False, now=timestamp(base, 35), error="demo timeout")
    database.record_probe("device", storage, False, now=timestamp(base, 36), error="demo timeout")
    database.record_probe("device", storage, True, 3.1, timestamp(base, 48))

    printer_b = ids["Printer B"]
    database.record_probe("device", printer_b, False, now=timestamp(base, 70), error="demo device offline")
    database.record_probe("device", printer_b, False, now=timestamp(base, 71), error="demo device offline")

    internet = database.create_target("Demo internet edge", "1.1.1.1", now=timestamp(base, 2))
    dns = database.create_target("Demo DNS", "8.8.8.8", kind="dns", query="example.com", now=timestamp(base, 3))
    database.record_probe("target", internet, True, 12.4, timestamp(base, 60))
    database.record_probe("target", dns, True, 18.7, timestamp(base, 61))
    database.create_printer(
        "Mock FDM printer",
        "Portfolio Demo",
        "Synthetic 300",
        "mock",
        device_id=ids["Printer A"],
        camera_enabled=True,
        now=timestamp(base, 20),
    )
    database.set_meta("collector_last_seen", timestamp(base, 89))
    database.set_meta("last_discovery", {"finished_at": timestamp(base, 88), "hosts": len(DEVICES), "mode": "synthetic_demo"})
    return database


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a deterministic PiSentinel portfolio database")
    parser.add_argument("--output", type=Path, default=Path("data/demo-pisentinel.db"))
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    database = build_demo_database(args.output, replace=args.replace)
    print(f"Synthetic PiSentinel database: {database.path}")
    print(f"Devices: {len(database.list_devices())}; incidents: {len(database.list_incidents())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
