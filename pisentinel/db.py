from __future__ import annotations

import ipaddress
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .config import GROUPS, validate_host
from .device_enrichment import COMMON_TCP_SERVICES, mac_address_type


def utc_datetime(value: str | datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_timestamp(value: str | datetime | None = None) -> str:
    return utc_datetime(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Database:
    """Short-lived WAL connections, shared by the API and collector processes.

    Discovery identifies a device by MAC. If an IP changes owners, the former
    owner keeps its labels and history but its current IP becomes null.
    Metadata values are JSON. Collector heartbeat key: ``collector_last_seen``.
    Discovery completion key: ``last_discovery`` (timestamp or structured data).
    """

    def __init__(self, path: str | Path, failure_threshold: int = 3,
                 recovery_threshold: int = 2, retention_days: int = 30,
                 incident_retention_days: int = 90):
        self.path = Path(path)
        self.failure_threshold = failure_threshold
        self.recovery_threshold = recovery_threshold
        self.retention_days = retention_days
        self.incident_retention_days = incident_retention_days
        if min(failure_threshold, recovery_threshold, retention_days, incident_retention_days) < 1:
            raise ValueError("Thresholds and retention must be positive.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY,
                    ip TEXT,
                    mac TEXT,
                    hostname TEXT,
                    name TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    group_name TEXT NOT NULL DEFAULT 'sem_grupo',
                    group_customized INTEGER NOT NULL DEFAULT 0 CHECK(group_customized IN (0,1)),
                    auto_name TEXT,
                    auto_group TEXT NOT NULL DEFAULT 'sem_grupo',
                    vendor TEXT,
                    identification_source TEXT,
                    identified_at TEXT,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    last_seen TEXT,
                    last_checked TEXT,
                    rtt_ms REAL,
                    failure_streak INTEGER NOT NULL DEFAULT 0,
                    success_streak INTEGER NOT NULL DEFAULT 0,
                    first_failed_at TEXT,
                    ever_online INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS devices_ip ON devices(ip) WHERE ip IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS devices_mac ON devices(mac) WHERE mac IS NOT NULL;
                CREATE TABLE IF NOT EXISTS device_services (
                    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    transport TEXT NOT NULL CHECK(transport IN ('tcp')),
                    port INTEGER NOT NULL CHECK(port BETWEEN 1 AND 65535),
                    service TEXT NOT NULL,
                    latency_ms REAL,
                    http_status INTEGER,
                    server TEXT,
                    content_type TEXT,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(device_id,transport,port)
                );
                CREATE TABLE IF NOT EXISTS router_device_observations (
                    device_id INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                    router_online INTEGER NOT NULL CHECK(router_online IN (0,1)),
                    interface TEXT NOT NULL,
                    ssid TEXT,
                    rssi_dbm INTEGER,
                    phy_mode TEXT,
                    power_save INTEGER CHECK(power_save IN (0,1)),
                    short_guard_interval INTEGER CHECK(short_guard_interval IN (0,1)),
                    stbc INTEGER CHECK(stbc IN (0,1)),
                    mu_beamforming INTEGER CHECK(mu_beamforming IN (0,1)),
                    spatial_streams INTEGER,
                    channel_width_mhz INTEGER,
                    tx_rate_mbps REAL,
                    rx_rate_mbps REAL,
                    connected_seconds INTEGER,
                    ip_method TEXT,
                    dhcp_expires_seconds INTEGER,
                    internet_allowed INTEGER CHECK(internet_allowed IN (0,1)),
                    router_device_type INTEGER,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS router_samples (
                    id INTEGER PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS router_samples_timestamp ON router_samples(timestamp);
                CREATE TABLE IF NOT EXISTS router_wifi_events (
                    event_key TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
                    mac TEXT NOT NULL,
                    event TEXT NOT NULL CHECK(event IN
                        ('authentication','associated','reassociated','disconnected','deauthenticated')),
                    reason TEXT,
                    rssi_dbm INTEGER
                );
                CREATE INDEX IF NOT EXISTS router_wifi_events_timestamp ON router_wifi_events(timestamp);
                CREATE INDEX IF NOT EXISTS router_wifi_events_device ON router_wifi_events(device_id,timestamp);
                CREATE TABLE IF NOT EXISTS printers (
                    id INTEGER PRIMARY KEY,
                    device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
                    name TEXT NOT NULL,
                    manufacturer TEXT NOT NULL,
                    model TEXT NOT NULL,
                    adapter_type TEXT NOT NULL,
                    adapter_config TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    camera_enabled INTEGER NOT NULL DEFAULT 0 CHECK(camera_enabled IN (0,1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS printers_device ON printers(device_id) WHERE device_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS targets (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK(scope IN ('local','external')),
                    kind TEXT NOT NULL CHECK(kind IN ('icmp','dns','tcp')),
                    port INTEGER,
                    query TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    last_seen TEXT,
                    last_checked TEXT,
                    rtt_ms REAL,
                    failure_streak INTEGER NOT NULL DEFAULT 0,
                    success_streak INTEGER NOT NULL DEFAULT 0,
                    first_failed_at TEXT,
                    ever_online INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY,
                    entity_type TEXT NOT NULL CHECK(entity_type IN ('device','target')),
                    entity_id INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    rtt_ms REAL,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS samples_entity_time ON samples(entity_type,entity_id,timestamp);
                CREATE INDEX IF NOT EXISTS samples_time ON samples(timestamp);
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY,
                    entity_type TEXT NOT NULL CHECK(entity_type IN ('device','target')),
                    entity_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    recovered_at TEXT,
                    ended_at TEXT,
                    end_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS incidents_time ON incidents(started_at);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'admin' CHECK(role IN ('admin')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0,1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    id INTEGER PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('admin','guest')),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_sessions_expiry ON auth_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS auth_login_attempts (
                    subject TEXT PRIMARY KEY,
                    failure_count INTEGER NOT NULL,
                    first_failed_at TEXT NOT NULL,
                    locked_until TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discovery_jobs (
                    id INTEGER PRIMARY KEY,
                    requested_at TEXT NOT NULL,
                    consumed_at TEXT,
                    status TEXT NOT NULL DEFAULT 'queued'
                );
                CREATE TABLE IF NOT EXISTS speedtest_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    interval_minutes INTEGER NOT NULL DEFAULT 360 CHECK(interval_minutes BETWEEN 60 AND 10080),
                    download_min_mbps REAL,
                    upload_min_mbps REAL,
                    failure_threshold INTEGER NOT NULL DEFAULT 2 CHECK(failure_threshold BETWEEN 1 AND 10),
                    recovery_threshold INTEGER NOT NULL DEFAULT 1 CHECK(recovery_threshold BETWEEN 1 AND 10),
                    retention_days INTEGER NOT NULL DEFAULT 90 CHECK(retention_days BETWEEN 1 AND 365),
                    next_run_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS speedtest_jobs (
                    id INTEGER PRIMARY KEY,
                    trigger TEXT NOT NULL CHECK(trigger IN ('manual','scheduled')),
                    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','completed','failed')),
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    result_id INTEGER REFERENCES speedtest_results(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS speedtest_jobs_status ON speedtest_jobs(status,id);
                CREATE TABLE IF NOT EXISTS speedtest_results (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER REFERENCES speedtest_jobs(id) ON DELETE SET NULL,
                    trigger TEXT NOT NULL CHECK(trigger IN ('manual','scheduled')),
                    success INTEGER NOT NULL CHECK(success IN (0,1)),
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    download_mbps REAL,
                    upload_mbps REAL,
                    ping_ms REAL,
                    jitter_ms REAL,
                    packet_loss_pct REAL,
                    download_bytes INTEGER,
                    upload_bytes INTEGER,
                    total_bytes INTEGER,
                    server_id TEXT,
                    server_name TEXT,
                    server_location TEXT,
                    server_country TEXT,
                    isp TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS speedtest_results_time ON speedtest_results(completed_at DESC);
                CREATE TABLE IF NOT EXISTS speedtest_incidents (
                    id INTEGER PRIMARY KEY,
                    metric TEXT NOT NULL CHECK(metric IN ('availability','download','upload')),
                    name TEXT NOT NULL,
                    threshold_mbps REAL,
                    started_at TEXT NOT NULL,
                    recovered_at TEXT,
                    ended_at TEXT,
                    end_reason TEXT,
                    last_result_id INTEGER REFERENCES speedtest_results(id) ON DELETE SET NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS speedtest_incidents_active ON speedtest_incidents(metric)
                    WHERE recovered_at IS NULL AND ended_at IS NULL;
                CREATE TABLE IF NOT EXISTS speedtest_metric_state (
                    metric TEXT PRIMARY KEY CHECK(metric IN ('availability','download','upload')),
                    failure_streak INTEGER NOT NULL DEFAULT 0,
                    success_streak INTEGER NOT NULL DEFAULT 0,
                    first_failed_at TEXT
                );
            """)
            now = utc_timestamp()
            conn.execute("""INSERT OR IGNORE INTO speedtest_config
                (id,enabled,interval_minutes,failure_threshold,recovery_threshold,retention_days,updated_at)
                VALUES (1,1,360,2,1,90,?)""", (now,))
            for metric in ("availability", "download", "upload"):
                conn.execute("INSERT OR IGNORE INTO speedtest_metric_state(metric) VALUES (?)", (metric,))
            device_columns = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
            additions = {
                "group_customized": "INTEGER NOT NULL DEFAULT 0 CHECK(group_customized IN (0,1))",
                "auto_name": "TEXT",
                "auto_group": "TEXT NOT NULL DEFAULT 'sem_grupo'",
                "vendor": "TEXT",
                "identification_source": "TEXT",
                "identified_at": "TEXT",
                "services_checked_at": "TEXT",
                "services_error": "TEXT",
            }
            added_group_customized = "group_customized" not in device_columns
            for column, declaration in additions.items():
                if column not in device_columns:
                    conn.execute(f"ALTER TABLE devices ADD COLUMN {column} {declaration}")
            if added_group_customized:
                conn.execute("UPDATE devices SET group_customized=1 WHERE group_name<>'sem_grupo'")
            incident_columns = {row["name"] for row in conn.execute("PRAGMA table_info(incidents)")}
            for column in ("ended_at", "end_reason"):
                if column not in incident_columns:
                    conn.execute(f"ALTER TABLE incidents ADD COLUMN {column} TEXT")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS incidents_active ON incidents(entity_type,entity_id)
                            WHERE recovered_at IS NULL AND ended_at IS NULL""")
            # Early development databases used an index without the explicit
            # administrative end state. Keep their schema compatible.
            conn.execute("DROP INDEX IF EXISTS incidents_open")

    def create_user(self, username: str, password_hash: str, role: str = "admin",
                    must_change_password: bool = False) -> dict[str, Any]:
        timestamp = utc_timestamp()
        with self._connection() as conn:
            try:
                cursor = conn.execute("""INSERT INTO users
                    (username,password_hash,role,must_change_password,created_at,updated_at)
                    VALUES (?,?,?,?,?,?)""",
                    (username, password_hash, role, int(must_change_password), timestamp, timestamp))
            except sqlite3.IntegrityError as exc:
                raise ValueError("Esse usuário já existe.") from exc
            return dict(conn.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone())

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
            return dict(row) if row else None

    def update_user_password(self, user_id: int, password_hash: str) -> None:
        with self._connection() as conn:
            if not conn.execute("""UPDATE users SET password_hash=?,must_change_password=0,updated_at=?
                WHERE id=?""", (password_hash, utc_timestamp(), user_id)).rowcount:
                raise KeyError(user_id)
            conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))

    def record_user_login(self, user_id: int) -> None:
        with self._connection() as conn:
            conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (utc_timestamp(), user_id))

    def create_auth_session(self, token_hash: str, role: str, user_id: int | None,
                            created_at: str, expires_at: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (created_at,))
            conn.execute("""INSERT INTO auth_sessions
                (token_hash,user_id,role,created_at,expires_at,last_seen_at) VALUES (?,?,?,?,?,?)""",
                (token_hash, user_id, role, created_at, expires_at, created_at))

    def get_auth_session(self, token_hash: str) -> dict[str, Any] | None:
        now = utc_timestamp()
        with self._connection() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
            row = conn.execute("""SELECT sessions.*,users.username,users.active,users.must_change_password
                FROM auth_sessions AS sessions LEFT JOIN users ON users.id=sessions.user_id
                WHERE sessions.token_hash=?""", (token_hash,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE auth_sessions SET last_seen_at=? WHERE id=?", (now, row["id"]))
            return dict(row)

    def delete_auth_session(self, token_hash: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash,))

    def get_auth_login_attempt(self, subject: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM auth_login_attempts WHERE subject=?", (subject,)).fetchone()
            return dict(row) if row else None

    def record_auth_login_failure(self, subject: str, limit: int, window: timedelta,
                                  lockout: timedelta) -> dict[str, Any]:
        now = utc_datetime()
        timestamp = utc_timestamp(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM auth_login_attempts WHERE subject=?", (subject,)).fetchone()
            if not row or now - utc_datetime(row["first_failed_at"]) > window:
                count = 1
                first_failed = timestamp
            else:
                count = int(row["failure_count"]) + 1
                first_failed = row["first_failed_at"]
            locked_until = utc_timestamp(now + lockout) if count >= limit else None
            conn.execute("""INSERT INTO auth_login_attempts
                (subject,failure_count,first_failed_at,locked_until,updated_at) VALUES (?,?,?,?,?)
                ON CONFLICT(subject) DO UPDATE SET failure_count=excluded.failure_count,
                first_failed_at=excluded.first_failed_at,locked_until=excluded.locked_until,
                updated_at=excluded.updated_at""",
                (subject, count, first_failed, locked_until, timestamp))
            return {"failure_count": count, "first_failed_at": first_failed, "locked_until": locked_until}

    def clear_auth_login_attempt(self, subject: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM auth_login_attempts WHERE subject=?", (subject,))

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _table(entity_type: str) -> str:
        if entity_type not in {"device", "target"}:
            raise ValueError("entity_type must be device or target")
        return "devices" if entity_type == "device" else "targets"

    def upsert_device(self, ip: str, mac: str | None = None,
                      hostname: str | None = None, now: str | datetime | None = None) -> int:
        ip = str(ipaddress.ip_address(ip))
        timestamp = utc_timestamp(now)
        if mac:
            mac = self._normalize_mac(mac)
        else:
            mac = None
        if hostname:
            hostname = hostname.strip()[:253]
            if any(ord(char) < 32 for char in hostname):
                raise ValueError("Invalid hostname")
        else:
            hostname = None
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            by_ip = conn.execute("SELECT * FROM devices WHERE ip=?", (ip,)).fetchone()
            by_mac = conn.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone() if mac else None
            owner = by_mac
            if owner is None and by_ip is not None and (not mac or not by_ip["mac"]):
                owner = by_ip
            if by_ip is not None and (owner is None or by_ip["id"] != owner["id"]):
                conn.execute("""UPDATE devices SET ip=NULL,status='unknown',failure_streak=0,
                                success_streak=0,first_failed_at=NULL WHERE id=?""", (by_ip["id"],))
            if owner is not None:
                conn.execute("""UPDATE devices SET ip=?,mac=COALESCE(?,mac),
                                hostname=COALESCE(?,hostname),last_seen=? WHERE id=?""",
                             (ip, mac, hostname, timestamp, owner["id"]))
                return int(owner["id"])
            cursor = conn.execute("""INSERT INTO devices
                (ip,mac,hostname,status,last_seen,ever_online,created_at) VALUES (?,?,?,?,?,?,?)""",
                (ip, mac, hostname, "online" if mac else "unknown", timestamp, int(bool(mac)), timestamp))
            return int(cursor.lastrowid)

    @staticmethod
    def _normalize_mac(mac: str) -> str:
        normalized = mac.strip().lower().replace("-", ":")
        if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", normalized):
            raise ValueError("Invalid MAC address")
        return normalized

    def get_device_by_mac(self, mac: str) -> dict[str, Any]:
        normalized = self._normalize_mac(mac)
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM devices WHERE mac=?", (normalized,)).fetchone()
            if row is None:
                raise KeyError(normalized)
            return self._device(row)

    def update_device(self, entity_id: int, changes: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"name", "notes", "group"}
        if set(changes) - allowed:
            raise ValueError("Only name, notes and group can be edited")
        changes = dict(changes)
        for key, limit in (("name", 100), ("notes", 2000)):
            if key in changes:
                if not isinstance(changes[key], str) or len(changes[key]) > limit:
                    raise ValueError(f"Invalid {key}")
                changes[key] = changes[key].strip()
        if "group" in changes:
            if changes["group"] not in GROUPS:
                raise ValueError("Invalid group")
            changes["group_name"] = changes.pop("group")
            changes["group_customized"] = 1
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT id FROM devices WHERE id=?", (entity_id,)).fetchone():
                raise KeyError(entity_id)
            if changes:
                assignments = ",".join(f"{key}=?" for key in changes)
                conn.execute(f"UPDATE devices SET {assignments} WHERE id=?", (*changes.values(), entity_id))
            return self._device(conn.execute("SELECT * FROM devices WHERE id=?", (entity_id,)).fetchone())

    @staticmethod
    def _device(row: sqlite3.Row) -> dict[str, Any]:
        keys = ("id", "ip", "mac", "hostname", "name", "notes", "status", "last_seen", "last_checked", "rtt_ms",
                "failure_streak", "success_streak", "first_failed_at", "ever_online", "created_at",
                "services_checked_at", "services_error")
        result = {key: row[key] for key in keys}
        result.update({key: row[key] for key in ("auto_name", "auto_group", "vendor", "identification_source", "identified_at")})
        result["group_customized"] = bool(row["group_customized"])
        result["ever_online"] = bool(result["ever_online"])
        result["mac_address_type"] = mac_address_type(result["mac"])
        result["group"] = row["group_name"] if result["group_customized"] else (row["auto_group"] or row["group_name"])
        result["display_name"] = row["name"] or row["auto_name"] or row["hostname"] or ""
        return result

    def set_device_identification(self, entity_id: int, *, auto_name: str | None,
                                  auto_group: str, vendor: str | None, source: str,
                                  now: str | datetime | None = None) -> dict[str, Any]:
        if auto_group not in GROUPS:
            raise ValueError("Invalid automatic group")
        values = {"auto_name": auto_name, "vendor": vendor, "source": source}
        for key, limit in (("auto_name", 100), ("vendor", 160), ("source", 80)):
            value = values[key]
            if value is not None and (not isinstance(value, str) or len(value.strip()) > limit):
                raise ValueError(f"Invalid {key}")
            values[key] = value.strip() if isinstance(value, str) and value.strip() else None
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT id FROM devices WHERE id=?", (entity_id,)).fetchone():
                raise KeyError(entity_id)
            conn.execute("""UPDATE devices SET auto_name=?,auto_group=?,vendor=?,
                            identification_source=?,identified_at=? WHERE id=?""",
                         (values["auto_name"], auto_group, values["vendor"], values["source"],
                          utc_timestamp(now), entity_id))
            return self._device(conn.execute("SELECT * FROM devices WHERE id=?", (entity_id,)).fetchone())

    def list_devices(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            query = """SELECT * FROM devices
                       ORDER BY CASE status WHEN 'online' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
                       COALESCE(NULLIF(name,''),NULLIF(auto_name,''),hostname,ip,'') COLLATE NOCASE,id"""
            return [self._device(row) for row in conn.execute(query)]

    def set_device_services(self, entity_id: int, services: list[Mapping[str, Any]], *,
                            checked_at: str | datetime | None = None,
                            error: str | None = None) -> None:
        timestamp = utc_timestamp(checked_at)
        if error is not None:
            error = str(error).strip()[:500] or None
        normalized = []
        for service in services:
            transport = service.get("transport", "tcp")
            port = service.get("port")
            name = str(service.get("service") or "").strip()[:100]
            if transport != "tcp" or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 or not name:
                raise ValueError("Serviço de dispositivo inválido.")
            latency = service.get("latency_ms")
            if latency is not None and (isinstance(latency, bool) or not isinstance(latency, (int, float)) or not math.isfinite(latency) or latency < 0):
                raise ValueError("Latência de serviço inválida.")
            status = service.get("http_status")
            if status is not None and (isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599):
                raise ValueError("Status HTTP inválido.")
            headers = []
            for key in ("server", "content_type"):
                value = service.get(key)
                headers.append(str(value).strip()[:160] if value else None)
            normalized.append((entity_id, transport, port, name,
                               round(float(latency), 3) if latency is not None else None,
                               status, *headers, timestamp))
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT id FROM devices WHERE id=?", (entity_id,)).fetchone():
                raise KeyError(entity_id)
            conn.execute("DELETE FROM device_services WHERE device_id=?", (entity_id,))
            conn.executemany("""INSERT INTO device_services
                (device_id,transport,port,service,latency_ms,http_status,server,content_type,observed_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", normalized)
            conn.execute("UPDATE devices SET services_checked_at=?,services_error=? WHERE id=?",
                         (timestamp, error, entity_id))

    def device_details(self, entity_id: int, *, now: str | datetime | None = None) -> dict[str, Any]:
        current = utc_datetime(now)
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM devices WHERE id=?", (entity_id,)).fetchone()
            if row is None:
                raise KeyError(entity_id)
            device = self._device(row)
            services = [dict(item) for item in conn.execute(
                """SELECT transport,port,service,latency_ms,http_status,server,content_type,observed_at
                   FROM device_services WHERE device_id=? ORDER BY port""", (entity_id,)
            )]
            router_row = conn.execute(
                "SELECT * FROM router_device_observations WHERE device_id=?", (entity_id,)
            ).fetchone()
            router_info = dict(router_row) if router_row else None
            if router_info:
                router_info.pop("device_id", None)
                for key in ("router_online", "power_save", "short_guard_interval", "stbc",
                            "mu_beamforming", "internet_allowed"):
                    if router_info.get(key) is not None:
                        router_info[key] = bool(router_info[key])
            availability = {}
            for label, hours in (("1h", 1), ("24h", 24), ("7d", 24 * 7)):
                since = utc_timestamp(current - timedelta(hours=hours))
                stats = conn.execute("""SELECT COUNT(*) AS samples,
                    COALESCE(SUM(success),0) AS successes,
                    AVG(CASE WHEN success=1 THEN rtt_ms END) AS avg_rtt_ms,
                    MIN(CASE WHEN success=1 THEN rtt_ms END) AS min_rtt_ms,
                    MAX(CASE WHEN success=1 THEN rtt_ms END) AS max_rtt_ms
                    FROM samples WHERE entity_type='device' AND entity_id=? AND timestamp>=?""",
                    (entity_id, since)).fetchone()
                samples = int(stats["samples"])
                successes = int(stats["successes"])
                availability[label] = {
                    "samples": samples,
                    "successes": successes,
                    "loss_pct": round((samples - successes) * 100 / samples, 2) if samples else None,
                    "avg_rtt_ms": round(stats["avg_rtt_ms"], 3) if stats["avg_rtt_ms"] is not None else None,
                    "min_rtt_ms": round(stats["min_rtt_ms"], 3) if stats["min_rtt_ms"] is not None else None,
                    "max_rtt_ms": round(stats["max_rtt_ms"], 3) if stats["max_rtt_ms"] is not None else None,
                }
            incidents = []
            for incident in conn.execute("""SELECT * FROM incidents WHERE entity_type='device' AND entity_id=?
                                            ORDER BY started_at DESC,id DESC LIMIT 10""", (entity_id,)):
                item = dict(incident)
                end_value = item.get("recovered_at") or item.get("ended_at")
                end = utc_datetime(end_value) if end_value else current
                item["duration_seconds"] = max(0, int((end - utc_datetime(item["started_at"])).total_seconds()))
                item["ongoing"] = not bool(end_value)
                incidents.append(item)
            return {**device, "services": services, "router": router_info, "availability": availability,
                    "recent_incidents": incidents, "checked_tcp_ports": len(COMMON_TCP_SERVICES)}

    @staticmethod
    def _validated_printer(data: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"device_id", "name", "manufacturer", "model", "adapter_type",
                   "adapter_config", "enabled", "camera_enabled"}
        if set(data) - allowed:
            raise ValueError("Unknown printer fields")
        result = dict(data)
        device_id = result.get("device_id")
        if device_id is not None and (isinstance(device_id, bool) or not isinstance(device_id, int) or device_id < 1):
            raise ValueError("device_id deve ser um identificador válido.")
        for key, limit in (("name", 100), ("manufacturer", 100), ("model", 100)):
            value = result.get(key)
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
                raise ValueError(f"{key} deve ter entre 1 e {limit} caracteres.")
            result[key] = value.strip()
        adapter_type = result.get("adapter_type")
        if not isinstance(adapter_type, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}", adapter_type):
            raise ValueError("adapter_type inválido.")
        result["adapter_type"] = adapter_type
        for key in ("enabled", "camera_enabled"):
            if not isinstance(result.get(key), bool):
                raise ValueError(f"{key} deve ser booleano.")
        config = result.get("adapter_config") or {}
        if not isinstance(config, Mapping):
            raise ValueError("adapter_config deve ser um objeto JSON.")
        encoded = json.dumps(dict(config), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded) > 4096:
            raise ValueError("adapter_config excede 4 KiB.")
        result["adapter_config"] = encoded
        return result

    def create_printer(self, name: str, manufacturer: str, model: str, adapter_type: str,
                       device_id: int | None = None, enabled: bool = True,
                       camera_enabled: bool = False, adapter_config: Mapping[str, Any] | None = None,
                       now: str | datetime | None = None) -> int:
        data = self._validated_printer({
            "device_id": device_id,
            "name": name,
            "manufacturer": manufacturer,
            "model": model,
            "adapter_type": adapter_type,
            "adapter_config": adapter_config or {},
            "enabled": enabled,
            "camera_enabled": camera_enabled,
        })
        timestamp = utc_timestamp(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if device_id is not None and not conn.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone():
                raise ValueError("Dispositivo associado não encontrado.")
            if conn.execute("SELECT COUNT(*) FROM printers").fetchone()[0] >= 128:
                raise ValueError("Limite de 128 impressoras atingido.")
            try:
                cursor = conn.execute("""INSERT INTO printers
                    (device_id,name,manufacturer,model,adapter_type,adapter_config,enabled,camera_enabled,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (*(data[key] for key in ("device_id", "name", "manufacturer", "model", "adapter_type",
                                             "adapter_config", "enabled", "camera_enabled")), timestamp, timestamp))
            except sqlite3.IntegrityError as exc:
                raise ValueError("O dispositivo já está associado a outra impressora.") from exc
            return int(cursor.lastrowid)

    def ensure_printer(self, name: str, manufacturer: str, model: str, adapter_type: str,
                       device_id: int, enabled: bool = True, camera_enabled: bool = False,
                       adapter_config: Mapping[str, Any] | None = None,
                       now: str | datetime | None = None) -> int:
        """Create or update the single printer linked to a known device."""
        data = self._validated_printer({
            "device_id": device_id,
            "name": name,
            "manufacturer": manufacturer,
            "model": model,
            "adapter_type": adapter_type,
            "adapter_config": adapter_config or {},
            "enabled": enabled,
            "camera_enabled": camera_enabled,
        })
        timestamp = utc_timestamp(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone():
                raise ValueError("Dispositivo associado não encontrado.")
            existing = conn.execute("SELECT id FROM printers WHERE device_id=?", (device_id,)).fetchone()
            if existing:
                conn.execute("""UPDATE printers SET name=?,manufacturer=?,model=?,adapter_type=?,
                             adapter_config=?,enabled=?,camera_enabled=?,updated_at=? WHERE id=?""",
                             (*(data[key] for key in ("name", "manufacturer", "model", "adapter_type",
                                                      "adapter_config", "enabled", "camera_enabled")),
                              timestamp, existing["id"]))
                return int(existing["id"])
            if conn.execute("SELECT COUNT(*) FROM printers").fetchone()[0] >= 128:
                raise ValueError("Limite de 128 impressoras atingido.")
            cursor = conn.execute("""INSERT INTO printers
                (device_id,name,manufacturer,model,adapter_type,adapter_config,enabled,camera_enabled,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (*(data[key] for key in ("device_id", "name", "manufacturer", "model", "adapter_type",
                                         "adapter_config", "enabled", "camera_enabled")), timestamp, timestamp))
            return int(cursor.lastrowid)

    @staticmethod
    def _printer(row: sqlite3.Row) -> dict[str, Any]:
        keys = ("id", "device_id", "name", "manufacturer", "model", "adapter_type",
                "created_at", "updated_at")
        result = {key: row[key] for key in keys}
        result["enabled"] = bool(row["enabled"])
        result["camera_enabled"] = bool(row["camera_enabled"])
        try:
            config = json.loads(row["adapter_config"])
        except (TypeError, json.JSONDecodeError):
            config = {}
        result["adapter_config"] = config if isinstance(config, dict) else {}
        result["ip"] = row["device_ip"]
        result["mac"] = row["device_mac"]
        result["network_status"] = row["device_status"]
        result["network_last_seen"] = row["device_last_seen"]
        return result

    @staticmethod
    def _printer_query() -> str:
        return """SELECT p.*,d.ip AS device_ip,d.mac AS device_mac,d.status AS device_status,
                  d.last_seen AS device_last_seen FROM printers p
                  LEFT JOIN devices d ON d.id=p.device_id"""

    def list_printers(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            return [self._printer(row) for row in conn.execute(self._printer_query() + " ORDER BY p.id")]

    def get_printer(self, printer_id: int) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(self._printer_query() + " WHERE p.id=?", (printer_id,)).fetchone()
            if row is None:
                raise KeyError(printer_id)
            return self._printer(row)

    @staticmethod
    def _validated_target(data: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"name", "host", "scope", "kind", "port", "query", "enabled"}
        if set(data) - allowed:
            raise ValueError("Unknown target fields")
        result = dict(data)
        if not isinstance(result.get("name"), str) or not 1 <= len(result["name"].strip()) <= 100:
            raise ValueError("Nome deve ter entre 1 e 100 caracteres.")
        result["name"] = result["name"].strip()
        result["host"] = validate_host(result["host"])
        if result["scope"] not in {"local", "external"} or result["kind"] not in {"icmp", "dns", "tcp"}:
            raise ValueError("Tipo ou abrangência inválida.")
        if not isinstance(result["enabled"], bool):
            raise ValueError("enabled deve ser booleano.")
        port = result.get("port")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
            raise ValueError("Porta deve estar entre 1 e 65535.")
        if result["kind"] == "tcp" and port is None:
            raise ValueError("Um destino TCP requer uma porta.")
        if result["kind"] == "dns":
            if not result.get("query"):
                raise ValueError("Um destino DNS requer um nome para consultar.")
            result["query"] = validate_host(result["query"])
            result["port"] = port or 53
        elif result.get("query") is not None:
            raise ValueError("query é permitida somente para DNS.")
        if result["kind"] == "icmp" and port is not None:
            raise ValueError("ICMP não usa porta.")
        return result

    def create_target(self, name: str, host: str, scope: str = "external", kind: str = "icmp",
                      port: int | None = None, query: str | None = None, enabled: bool = True,
                      now: str | datetime | None = None) -> int:
        data = self._validated_target(dict(name=name, host=host, scope=scope, kind=kind,
                                          port=port, query=query, enabled=enabled))
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0] >= 128:
                raise ValueError("Limite de 128 destinos atingido.")
            cursor = conn.execute("""INSERT INTO targets
                (name,host,scope,kind,port,query,enabled,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (*(data[key] for key in ("name", "host", "scope", "kind", "port", "query", "enabled")), utc_timestamp(now)))
            return int(cursor.lastrowid)

    def update_target(self, entity_id: int, changes: Mapping[str, Any],
                      now: str | datetime | None = None) -> dict[str, Any]:
        timestamp = utc_timestamp(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM targets WHERE id=?", (entity_id,)).fetchone()
            if row is None:
                raise KeyError(entity_id)
            keys = ("name", "host", "scope", "kind", "port", "query", "enabled")
            existing = {key: bool(row[key]) if key == "enabled" else row[key] for key in keys}
            data = self._validated_target({**existing, **changes})
            identity_changed = any(data[key] != existing[key] for key in ("host", "kind", "port", "query"))
            if identity_changed:
                # Reconfiguration is not recovery. Preserve historical outages
                # with an administrative end instead of inventing a success.
                conn.execute("""UPDATE incidents SET ended_at=?,end_reason='target_changed'
                    WHERE entity_type='target' AND entity_id=? AND recovered_at IS NULL AND ended_at IS NULL""", (timestamp, entity_id))
                conn.execute("""UPDATE targets SET status='unknown',rtt_ms=NULL,last_seen=NULL,
                    last_checked=NULL,failure_streak=0,success_streak=0,first_failed_at=NULL,ever_online=0 WHERE id=?""", (entity_id,))
                conn.execute("DELETE FROM samples WHERE entity_type='target' AND entity_id=?", (entity_id,))
            elif data["enabled"] != existing["enabled"]:
                # Pausing monitoring is not a successful probe. Keep any real
                # incident open until it can be checked again after resuming.
                conn.execute("""UPDATE targets SET failure_streak=0,success_streak=0,
                    first_failed_at=NULL WHERE id=?""", (entity_id,))
            conn.execute("UPDATE targets SET " + ",".join(f"{key}=?" for key in keys) + " WHERE id=?",
                         (*(data[key] for key in keys), entity_id))
            return self._target(conn, conn.execute("SELECT * FROM targets WHERE id=?", (entity_id,)).fetchone(), timestamp)

    def delete_target(self, entity_id: int) -> bool:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("DELETE FROM targets WHERE id=?", (entity_id,))
            if cursor.rowcount:
                conn.execute("DELETE FROM samples WHERE entity_type='target' AND entity_id=?", (entity_id,))
                conn.execute("DELETE FROM incidents WHERE entity_type='target' AND entity_id=?", (entity_id,))
            return bool(cursor.rowcount)

    @staticmethod
    def _target(conn: sqlite3.Connection, row: sqlite3.Row, now: str) -> dict[str, Any]:
        keys = ("id", "name", "host", "scope", "kind", "port", "query", "status", "last_seen", "last_checked", "rtt_ms")
        result = {key: row[key] for key in keys}
        result["enabled"] = bool(row["enabled"])
        since = utc_timestamp(utc_datetime(now) - timedelta(hours=24))
        sample = conn.execute("""SELECT COUNT(*) AS count,SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS lost
            FROM samples WHERE entity_type='target' AND entity_id=? AND timestamp>=? AND timestamp<=?""",
            (row["id"], since, now)).fetchone()
        result["samples_count"] = sample["count"]
        result["loss_pct"] = round(100 * (sample["lost"] or 0) / sample["count"], 2) if sample["count"] else None
        if not result["enabled"]:
            result["status"] = "unknown"
        return result

    def list_targets(self, now: str | datetime | None = None) -> list[dict[str, Any]]:
        timestamp = utc_timestamp(now)
        with self._connection() as conn:
            return [self._target(conn, row, timestamp) for row in conn.execute("SELECT * FROM targets ORDER BY id")]

    def record_probe(self, entity_type: str, entity_id: int, success: bool,
                     rtt_ms: float | None = None, now: str | datetime | None = None,
                     error: str | None = None) -> dict[str, Any]:
        table = self._table(entity_type)
        timestamp = utc_timestamp(now)
        if not isinstance(success, bool):
            raise ValueError("success must be bool")
        if rtt_ms is not None and (not math.isfinite(rtt_ms) or not 0 <= rtt_ms <= 600000):
            raise ValueError("Invalid RTT")
        if not success:
            rtt_ms = None
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
            if row is None:
                raise KeyError(entity_id)
            if entity_type == "target" and not row["enabled"]:
                return {"status": "unknown", "changed": False, "skipped": True, "incident_id": None}
            if row["last_checked"] and timestamp < row["last_checked"]:
                raise ValueError("Probe timestamp precedes the last observation")
            conn.execute("INSERT INTO samples(entity_type,entity_id,timestamp,success,rtt_ms,error) VALUES (?,?,?,?,?,?)",
                         (entity_type, entity_id, timestamp, int(success), rtt_ms, str(error)[:500] if error else None))
            failure_streak = 0 if success else row["failure_streak"] + 1
            success_streak = row["success_streak"] + 1 if success else 0
            first_failed = None if success else row["first_failed_at"] or timestamp
            status = row["status"]
            ever_online = row["ever_online"]
            incident_id = None
            if success and success_streak >= self.recovery_threshold:
                status = "online"
                ever_online = 1
                conn.execute("UPDATE incidents SET recovered_at=? WHERE entity_type=? AND entity_id=? AND recovered_at IS NULL AND ended_at IS NULL",
                             (timestamp, entity_type, entity_id))
            elif not success and failure_streak >= self.failure_threshold:
                status = "offline"
                if ever_online:
                    name = row["name"] or (row["hostname"] if entity_type == "device" else None) or row["ip" if entity_type == "device" else "host"] or f"Dispositivo {entity_id}"
                    conn.execute("INSERT OR IGNORE INTO incidents(entity_type,entity_id,name,started_at) VALUES (?,?,?,?)",
                                 (entity_type, entity_id, name, first_failed))
            conn.execute(f"""UPDATE {table} SET status=?,last_seen=?,last_checked=?,rtt_ms=?,
                failure_streak=?,success_streak=?,first_failed_at=?,ever_online=? WHERE id=?""",
                (status, timestamp if success else row["last_seen"], timestamp, rtt_ms,
                 failure_streak, success_streak, first_failed, ever_online, entity_id))
            incident = conn.execute("SELECT id FROM incidents WHERE entity_type=? AND entity_id=? AND recovered_at IS NULL AND ended_at IS NULL",
                                    (entity_type, entity_id)).fetchone()
            if incident:
                incident_id = incident["id"]
            return {"status": status, "changed": status != row["status"], "incident_id": incident_id,
                    "failure_streak": failure_streak, "success_streak": success_streak}

    def target_history(self, entity_id: int, hours: int = 24,
                       now: str | datetime | None = None) -> list[dict[str, Any]]:
        if not 1 <= hours <= 24 * self.retention_days:
            raise ValueError("History range exceeds retention")
        timestamp = utc_timestamp(now)
        since = utc_timestamp(utc_datetime(now) - timedelta(hours=hours))
        with self._connection() as conn:
            if not conn.execute("SELECT id FROM targets WHERE id=?", (entity_id,)).fetchone():
                raise KeyError(entity_id)
            return [{"timestamp": row["timestamp"], "success": bool(row["success"]), "rtt_ms": row["rtt_ms"]}
                    for row in conn.execute("""SELECT timestamp,success,rtt_ms FROM samples WHERE entity_type='target'
                        AND entity_id=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp,id""", (entity_id, since, timestamp))]

    def list_incidents(self, limit: int = 100, now: str | datetime | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        current = utc_datetime(now)
        with self._connection() as conn:
            results = []
            for row in conn.execute("SELECT * FROM incidents ORDER BY started_at DESC,id DESC LIMIT ?", (limit,)):
                item = dict(row)
                ended_at = item.get("ended_at")
                end = utc_datetime(item["recovered_at"] or ended_at) if item["recovered_at"] or ended_at else current
                item["duration_seconds"] = max(0, int((end - utc_datetime(item["started_at"])).total_seconds()))
                item["ongoing"] = item["recovered_at"] is None and ended_at is None
                results.append(item)
            return results

    def open_incident_count(self) -> int:
        with self._connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM incidents WHERE recovered_at IS NULL AND ended_at IS NULL").fetchone()[0]

    def set_router_snapshot(self, router: Mapping[str, Any], clients: list[Mapping[str, Any]], *,
                            now: str | datetime | None = None) -> dict[str, Any]:
        """Persist one sanitized, read-only router observation.

        Credentials and ASUS session identifiers never enter SQLite.  Online
        router clients may seed the existing device inventory, while historical
        offline-only clients are linked only when the MAC already exists.
        """
        timestamp = utc_timestamp(now)
        snapshot = json.loads(json.dumps(dict(router), ensure_ascii=False, allow_nan=False))
        snapshot["observed_at"] = timestamp
        counters = snapshot.get("traffic_counters")
        previous = self.get_meta("router_traffic_previous", None)
        rates: dict[str, dict[str, float]] = {}
        if isinstance(counters, dict) and isinstance(previous, dict):
            try:
                elapsed = (utc_datetime(timestamp) - utc_datetime(previous["timestamp"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                elapsed = 0
            if elapsed > 0:
                for interface, values in counters.items():
                    old = previous.get("counters", {}).get(interface, {})
                    if not isinstance(values, dict) or not isinstance(old, dict):
                        continue
                    rx_delta = int(values.get("rx_bytes", 0)) - int(old.get("rx_bytes", 0))
                    tx_delta = int(values.get("tx_bytes", 0)) - int(old.get("tx_bytes", 0))
                    if rx_delta >= 0 and tx_delta >= 0:
                        rates[interface] = {
                            "rx_mbps": round(rx_delta * 8 / elapsed / 1_000_000, 3),
                            "tx_mbps": round(tx_delta * 8 / elapsed / 1_000_000, 3),
                        }
        snapshot["traffic_rates"] = rates
        if isinstance(counters, dict):
            self.set_meta("router_traffic_previous", {"timestamp": timestamp, "counters": counters})

        observations: list[tuple[Any, ...]] = []
        for raw in clients:
            mac = self._normalize_mac(str(raw.get("mac") or ""))
            ip = raw.get("ip")
            device_id: int | None = None
            if bool(raw.get("online")) and ip:
                try:
                    device_id = self.upsert_device(str(ip), mac, raw.get("hostname"), now=timestamp)
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    device_id = self.get_device_by_mac(mac)["id"]
                except KeyError:
                    continue
            interface = str(raw.get("interface") or "unknown")
            if interface not in {"wired", "wifi_2_4", "wifi_5", "unknown"}:
                interface = "unknown"
            observations.append((
                device_id, int(bool(raw.get("online"))), interface,
                str(raw.get("ssid"))[:64] if raw.get("ssid") else None,
                raw.get("rssi_dbm"), str(raw.get("phy_mode"))[:20] if raw.get("phy_mode") else None,
                int(bool(raw.get("power_save"))) if raw.get("power_save") is not None else None,
                int(bool(raw.get("short_guard_interval"))) if raw.get("short_guard_interval") is not None else None,
                int(bool(raw.get("stbc"))) if raw.get("stbc") is not None else None,
                int(bool(raw.get("mu_beamforming"))) if raw.get("mu_beamforming") is not None else None,
                raw.get("spatial_streams"), raw.get("channel_width_mhz"), raw.get("tx_rate_mbps"),
                raw.get("rx_rate_mbps"), raw.get("connected_seconds"),
                str(raw.get("ip_method"))[:20] if raw.get("ip_method") else None,
                raw.get("dhcp_expires_seconds"),
                int(bool(raw.get("internet_allowed"))) if raw.get("internet_allowed") is not None else None,
                raw.get("router_device_type"), timestamp,
            ))
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("""INSERT INTO router_device_observations
                (device_id,router_online,interface,ssid,rssi_dbm,phy_mode,power_save,
                 short_guard_interval,stbc,mu_beamforming,spatial_streams,channel_width_mhz,
                 tx_rate_mbps,rx_rate_mbps,connected_seconds,ip_method,dhcp_expires_seconds,
                 internet_allowed,router_device_type,observed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(device_id) DO UPDATE SET
                 router_online=excluded.router_online,interface=excluded.interface,ssid=excluded.ssid,
                 rssi_dbm=excluded.rssi_dbm,phy_mode=excluded.phy_mode,power_save=excluded.power_save,
                 short_guard_interval=excluded.short_guard_interval,stbc=excluded.stbc,
                 mu_beamforming=excluded.mu_beamforming,spatial_streams=excluded.spatial_streams,
                 channel_width_mhz=excluded.channel_width_mhz,tx_rate_mbps=excluded.tx_rate_mbps,
                 rx_rate_mbps=excluded.rx_rate_mbps,connected_seconds=excluded.connected_seconds,
                 ip_method=excluded.ip_method,dhcp_expires_seconds=excluded.dhcp_expires_seconds,
                 internet_allowed=excluded.internet_allowed,router_device_type=excluded.router_device_type,
                 observed_at=excluded.observed_at""", observations)
            encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
            conn.execute("INSERT INTO router_samples(timestamp,payload) VALUES (?,?)", (timestamp, encoded))
            conn.execute("""INSERT INTO metadata(key,value) VALUES ('router_status',?)
                            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (encoded,))
        return snapshot

    def set_router_error(self, error: str, *, now: str | datetime | None = None) -> None:
        current = self.get_meta("router_status", {})
        if not isinstance(current, dict):
            current = {}
        current.update({"available": False, "error": str(error)[:300], "observed_at": utc_timestamp(now)})
        self.set_meta("router_status", current)

    def record_router_wifi_events(self, events: list[Mapping[str, Any]]) -> int:
        """Store normalized Wi-Fi events without persisting the router's raw log."""
        allowed = {"authentication", "associated", "reassociated", "disconnected", "deauthenticated"}
        rows: list[tuple[Any, ...]] = []
        with self._connection() as conn:
            for raw in events:
                event_key = str(raw.get("event_key") or "").lower()
                event = str(raw.get("event") or "").lower()
                if not re.fullmatch(r"[0-9a-f]{64}", event_key) or event not in allowed:
                    continue
                try:
                    timestamp = utc_timestamp(raw.get("timestamp"))
                    mac = self._normalize_mac(str(raw.get("mac") or ""))
                except (TypeError, ValueError):
                    continue
                device = conn.execute("SELECT id FROM devices WHERE mac=?", (mac,)).fetchone()
                reason = str(raw.get("reason"))[:160] if raw.get("reason") else None
                rssi = raw.get("rssi_dbm")
                rows.append((event_key, timestamp, device["id"] if device else None,
                             mac, event, reason, int(rssi) if rssi is not None else None))
            if not rows:
                return 0
            before = conn.total_changes
            conn.executemany("""INSERT OR IGNORE INTO router_wifi_events
                (event_key,timestamp,device_id,mac,event,reason,rssi_dbm)
                VALUES (?,?,?,?,?,?,?)""", rows)
            return conn.total_changes - before

    def router_dashboard(self, now: str | datetime | None = None) -> dict[str, Any]:
        status = self.get_meta("router_status", {"available": False, "error": "Aguardando a primeira coleta."})
        cutoff = utc_timestamp(utc_datetime(now) - timedelta(hours=24))
        with self._connection() as conn:
            clients = []
            for row in conn.execute("""SELECT device_id FROM router_device_observations
                                       ORDER BY router_online DESC,rssi_dbm DESC,device_id"""):
                device = self._device(conn.execute("SELECT * FROM devices WHERE id=?", (row["device_id"],)).fetchone())
                observed = dict(conn.execute(
                    "SELECT * FROM router_device_observations WHERE device_id=?", (row["device_id"],)
                ).fetchone())
                observed.pop("device_id", None)
                for key in ("router_online", "power_save", "short_guard_interval", "stbc",
                            "mu_beamforming", "internet_allowed"):
                    if observed.get(key) is not None:
                        observed[key] = bool(observed[key])
                clients.append({
                    "device_id": device["id"], "display_name": device["display_name"],
                    "ip": device["ip"], "mac": device["mac"], "status": device["status"],
                    **observed,
                })
            wifi_events = [dict(row) for row in conn.execute("""SELECT e.timestamp,e.device_id,
                COALESCE(NULLIF(d.name,''),NULLIF(d.auto_name,''),NULLIF(d.hostname,''),d.ip,e.mac) AS display_name,
                e.mac,e.event,e.reason,e.rssi_dbm
                FROM router_wifi_events e LEFT JOIN devices d ON d.id=e.device_id
                ORDER BY e.timestamp DESC LIMIT 50""")]
            wifi_alerts = [dict(row) for row in conn.execute("""SELECT e.device_id,
                COALESCE(NULLIF(d.name,''),NULLIF(d.auto_name,''),NULLIF(d.hostname,''),d.ip,e.mac) AS display_name,
                e.mac,COUNT(*) AS event_count,MAX(e.timestamp) AS last_event_at,
                MIN(e.rssi_dbm) AS min_rssi_dbm,MAX(e.rssi_dbm) AS max_rssi_dbm
                FROM router_wifi_events e LEFT JOIN devices d ON d.id=e.device_id
                WHERE e.timestamp>=? AND e.event IN ('disconnected','deauthenticated')
                GROUP BY e.mac HAVING COUNT(*)>=3
                ORDER BY event_count DESC,last_event_at DESC""", (cutoff,))]
        return {"router": status, "clients": clients, "wifi_events": wifi_events,
                "wifi_alerts": wifi_alerts}

    def set_meta(self, key: str, value: Any) -> None:
        if not key or len(key) > 100:
            raise ValueError("Invalid metadata key")
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 65536:
            raise ValueError("Metadata value is too large")
        with self._connection() as conn:
            conn.execute("INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encoded))

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def request_discovery(self, now: str | datetime | None = None) -> dict[str, Any]:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM discovery_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                cursor = conn.execute("INSERT INTO discovery_jobs(requested_at) VALUES (?)", (utc_timestamp(now),))
                row = conn.execute("SELECT * FROM discovery_jobs WHERE id=?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def consume_discovery_request(self, now: str | datetime | None = None) -> dict[str, Any] | None:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM discovery_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            timestamp = utc_timestamp(now)
            conn.execute("UPDATE discovery_jobs SET status='consumed',consumed_at=? WHERE id=?", (timestamp, row["id"]))
            return {**dict(row), "status": "consumed", "consumed_at": timestamp}

    @staticmethod
    def _speedtest_config(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def get_speedtest_config(self) -> dict[str, Any]:
        with self._connection() as conn:
            return self._speedtest_config(conn.execute("SELECT * FROM speedtest_config WHERE id=1").fetchone())

    def update_speedtest_config(self, changes: Mapping[str, Any],
                                now: str | datetime | None = None) -> dict[str, Any]:
        allowed = {"enabled", "interval_minutes", "download_min_mbps", "upload_min_mbps",
                   "failure_threshold", "recovery_threshold", "retention_days"}
        if not changes or set(changes) - allowed:
            raise ValueError("Configuração de teste inválida.")
        values = dict(changes)
        if "enabled" in values and not isinstance(values["enabled"], bool):
            raise ValueError("enabled deve ser booleano.")
        for key, minimum, maximum in (("interval_minutes", 60, 10080), ("failure_threshold", 1, 10),
                                      ("recovery_threshold", 1, 10), ("retention_days", 1, 365)):
            if key in values and (isinstance(values[key], bool) or not isinstance(values[key], int)
                                  or not minimum <= values[key] <= maximum):
                raise ValueError(f"{key} deve estar entre {minimum} e {maximum}.")
        for key in ("download_min_mbps", "upload_min_mbps"):
            if key in values and values[key] is not None:
                value = values[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 100000:
                    raise ValueError(f"{key} deve ser nulo ou maior que zero.")
                values[key] = float(value)
        timestamp = utc_timestamp(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM speedtest_config WHERE id=1").fetchone()
            if "interval_minutes" in values and values["interval_minutes"] != current["interval_minutes"]:
                values["next_run_at"] = utc_timestamp(utc_datetime(timestamp) + timedelta(minutes=values["interval_minutes"]))
            elif values.get("enabled") is True and not current["enabled"]:
                values["next_run_at"] = utc_timestamp(utc_datetime(timestamp) + timedelta(minutes=current["interval_minutes"]))
            if values.get("enabled") is False:
                values["next_run_at"] = None
            values["updated_at"] = timestamp
            conn.execute("UPDATE speedtest_config SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=1", tuple(values.values()))
            for metric, key in (("download", "download_min_mbps"), ("upload", "upload_min_mbps")):
                if key in values and values[key] is None:
                    conn.execute("UPDATE speedtest_metric_state SET failure_streak=0,success_streak=0,first_failed_at=NULL WHERE metric=?", (metric,))
                    conn.execute("""UPDATE speedtest_incidents SET ended_at=?,end_reason='threshold_disabled'
                        WHERE metric=? AND recovered_at IS NULL AND ended_at IS NULL""", (timestamp, metric))
            return self._speedtest_config(conn.execute("SELECT * FROM speedtest_config WHERE id=1").fetchone())

    def request_speedtest(self, trigger: str = "manual", now: str | datetime | None = None) -> dict[str, Any]:
        if trigger not in {"manual", "scheduled"}:
            raise ValueError("Trigger inválido.")
        timestamp = utc_timestamp(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM speedtest_jobs WHERE status IN ('queued','running') ORDER BY id LIMIT 1").fetchone()
            if existing:
                return {**dict(existing), "already_pending": True}
            cursor = conn.execute("INSERT INTO speedtest_jobs(trigger,requested_at) VALUES (?,?)", (trigger, timestamp))
            row = conn.execute("SELECT * FROM speedtest_jobs WHERE id=?", (cursor.lastrowid,)).fetchone()
            return {**dict(row), "already_pending": False}

    def schedule_speedtest_if_due(self, now: str | datetime | None = None) -> dict[str, Any] | None:
        timestamp = utc_timestamp(now)
        current = utc_datetime(timestamp)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            config = conn.execute("SELECT * FROM speedtest_config WHERE id=1").fetchone()
            if not config["enabled"]:
                return None
            if not config["next_run_at"]:
                conn.execute("UPDATE speedtest_config SET next_run_at=? WHERE id=1",
                             (utc_timestamp(current + timedelta(minutes=config["interval_minutes"])),))
                return None
            if current < utc_datetime(config["next_run_at"]):
                return None
            next_run = utc_timestamp(current + timedelta(minutes=config["interval_minutes"]))
            conn.execute("UPDATE speedtest_config SET next_run_at=? WHERE id=1", (next_run,))
            existing = conn.execute("SELECT * FROM speedtest_jobs WHERE status IN ('queued','running') ORDER BY id LIMIT 1").fetchone()
            if existing:
                return {**dict(existing), "already_pending": True}
            cursor = conn.execute("INSERT INTO speedtest_jobs(trigger,requested_at) VALUES ('scheduled',?)", (timestamp,))
            return {**dict(conn.execute("SELECT * FROM speedtest_jobs WHERE id=?", (cursor.lastrowid,)).fetchone()),
                    "already_pending": False}

    def claim_speedtest_job(self, now: str | datetime | None = None) -> dict[str, Any] | None:
        timestamp = utc_timestamp(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM speedtest_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE speedtest_jobs SET status='running',started_at=? WHERE id=?", (timestamp, row["id"]))
            return {**dict(row), "status": "running", "started_at": timestamp}

    def recover_stale_speedtest_jobs(self, now: str | datetime | None = None, max_age_minutes: int = 15) -> int:
        cutoff = utc_timestamp(utc_datetime(now) - timedelta(minutes=max_age_minutes))
        with self._connection() as conn:
            return conn.execute("""UPDATE speedtest_jobs SET status='queued',started_at=NULL
                WHERE status='running' AND started_at<?""", (cutoff,)).rowcount

    def complete_speedtest_job(self, job_id: int, measurement: Mapping[str, Any]) -> dict[str, Any]:
        required = {"success", "started_at", "completed_at", "duration_seconds"}
        if not required <= set(measurement):
            raise ValueError("Resultado de teste incompleto.")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT * FROM speedtest_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["status"] != "running":
                raise KeyError(job_id)
            fields = ("success", "started_at", "completed_at", "duration_seconds", "download_mbps", "upload_mbps",
                      "ping_ms", "jitter_ms", "packet_loss_pct", "download_bytes", "upload_bytes", "total_bytes",
                      "server_id", "server_name", "server_location", "server_country", "isp", "error")
            data = {key: measurement.get(key) for key in fields}
            data["success"] = int(bool(data["success"]))
            if data["error"]:
                data["error"] = str(data["error"])[:500]
            cursor = conn.execute("""INSERT INTO speedtest_results
                (job_id,trigger,success,started_at,completed_at,duration_seconds,download_mbps,upload_mbps,ping_ms,
                 jitter_ms,packet_loss_pct,download_bytes,upload_bytes,total_bytes,server_id,server_name,server_location,
                 server_country,isp,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, job["trigger"], *(data[key] for key in fields)))
            result_id = int(cursor.lastrowid)
            status = "completed" if data["success"] else "failed"
            conn.execute("UPDATE speedtest_jobs SET status=?,completed_at=?,result_id=? WHERE id=?",
                         (status, data["completed_at"], result_id, job_id))
            config = conn.execute("SELECT * FROM speedtest_config WHERE id=1").fetchone()
            self._update_speedtest_incidents(conn, result_id, data, config)
            return dict(conn.execute("SELECT * FROM speedtest_results WHERE id=?", (result_id,)).fetchone())

    @staticmethod
    def _update_speedtest_incidents(conn: sqlite3.Connection, result_id: int,
                                    result: Mapping[str, Any], config: sqlite3.Row) -> None:
        checks: list[tuple[str, bool | None, float | None, str]] = [
            ("availability", bool(result["success"]), None, "Teste de velocidade indisponível"),
            ("download", None if not result["success"] or config["download_min_mbps"] is None
             else result["download_mbps"] is not None and result["download_mbps"] >= config["download_min_mbps"],
             config["download_min_mbps"], "Download abaixo do limite"),
            ("upload", None if not result["success"] or config["upload_min_mbps"] is None
             else result["upload_mbps"] is not None and result["upload_mbps"] >= config["upload_min_mbps"],
             config["upload_min_mbps"], "Upload abaixo do limite"),
        ]
        timestamp = result["completed_at"]
        for metric, healthy, threshold, name in checks:
            if healthy is None:
                continue
            state = conn.execute("SELECT * FROM speedtest_metric_state WHERE metric=?", (metric,)).fetchone()
            failure_streak = 0 if healthy else state["failure_streak"] + 1
            success_streak = state["success_streak"] + 1 if healthy else 0
            first_failed = None if healthy else state["first_failed_at"] or timestamp
            if not healthy and failure_streak >= config["failure_threshold"]:
                conn.execute("""INSERT OR IGNORE INTO speedtest_incidents
                    (metric,name,threshold_mbps,started_at,last_result_id) VALUES (?,?,?,?,?)""",
                    (metric, name, threshold, first_failed, result_id))
            elif healthy and success_streak >= config["recovery_threshold"]:
                conn.execute("""UPDATE speedtest_incidents SET recovered_at=?,last_result_id=?
                    WHERE metric=? AND recovered_at IS NULL AND ended_at IS NULL""", (timestamp, result_id, metric))
            conn.execute("""UPDATE speedtest_metric_state SET failure_streak=?,success_streak=?,first_failed_at=?
                WHERE metric=?""", (failure_streak, success_streak, first_failed, metric))

    def speedtest_dashboard(self, days: int = 7, now: str | datetime | None = None) -> dict[str, Any]:
        if days not in {1, 7, 30}:
            raise ValueError("Período deve ser 1, 7 ou 30 dias.")
        current = utc_datetime(now)
        since = utc_timestamp(current - timedelta(days=days))
        month_start = utc_timestamp(current.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
        with self._connection() as conn:
            config = self._speedtest_config(conn.execute("SELECT * FROM speedtest_config WHERE id=1").fetchone())
            history = [dict(row) for row in conn.execute(
                "SELECT * FROM speedtest_results WHERE completed_at>=? ORDER BY completed_at,id", (since,))]
            last = conn.execute("SELECT * FROM speedtest_results ORDER BY completed_at DESC,id DESC LIMIT 1").fetchone()
            active = conn.execute("SELECT id,trigger,status,requested_at,started_at FROM speedtest_jobs WHERE status IN ('queued','running') ORDER BY id LIMIT 1").fetchone()
            usage = conn.execute("SELECT COALESCE(SUM(total_bytes),0) FROM speedtest_results WHERE success=1 AND completed_at>=?", (month_start,)).fetchone()[0]
            incidents = []
            for row in conn.execute("SELECT * FROM speedtest_incidents ORDER BY started_at DESC,id DESC LIMIT 100"):
                item = dict(row)
                end = utc_datetime(item["recovered_at"] or item["ended_at"]) if item["recovered_at"] or item["ended_at"] else current
                item["duration_seconds"] = max(0, int((end - utc_datetime(item["started_at"])).total_seconds()))
                item["ongoing"] = item["recovered_at"] is None and item["ended_at"] is None
                incidents.append(item)
            return {"scope": "internet", "provider": "Ookla", "period_days": days, "config": config,
                    "active_job": dict(active) if active else None, "last_result": dict(last) if last else None,
                    "history": history, "incidents": incidents, "month_total_bytes": int(usage or 0)}

    def prune_speedtests(self, now: str | datetime | None = None) -> dict[str, int]:
        current = utc_datetime(now)
        with self._connection() as conn:
            retention = conn.execute("SELECT retention_days FROM speedtest_config WHERE id=1").fetchone()[0]
            cutoff = utc_timestamp(current - timedelta(days=retention))
            results = conn.execute("DELETE FROM speedtest_results WHERE completed_at<?", (cutoff,)).rowcount
            jobs = conn.execute("DELETE FROM speedtest_jobs WHERE completed_at IS NOT NULL AND completed_at<?", (cutoff,)).rowcount
            incidents = conn.execute("DELETE FROM speedtest_incidents WHERE COALESCE(recovered_at,ended_at)<?", (cutoff,)).rowcount
            return {"results": results, "jobs": jobs, "incidents": incidents}

    def prune(self, now: str | datetime | None = None) -> dict[str, int]:
        current = utc_datetime(now)
        sample_cutoff = utc_timestamp(current - timedelta(days=self.retention_days))
        incident_cutoff = utc_timestamp(current - timedelta(days=self.incident_retention_days))
        with self._connection() as conn:
            samples = conn.execute("DELETE FROM samples WHERE timestamp<?", (sample_cutoff,)).rowcount
            router_samples = conn.execute("DELETE FROM router_samples WHERE timestamp<?", (sample_cutoff,)).rowcount
            router_wifi_events = conn.execute("DELETE FROM router_wifi_events WHERE timestamp<?", (sample_cutoff,)).rowcount
            incidents = conn.execute("DELETE FROM incidents WHERE COALESCE(recovered_at,ended_at)<?", (incident_cutoff,)).rowcount
            jobs = conn.execute("DELETE FROM discovery_jobs WHERE status='consumed' AND consumed_at<?", (sample_cutoff,)).rowcount
            return {"samples": samples, "router_samples": router_samples,
                    "router_wifi_events": router_wifi_events,
                    "incidents": incidents, "discovery_jobs": jobs}
