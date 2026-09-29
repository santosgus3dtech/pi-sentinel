from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import time
from pathlib import Path

from .config import Settings
from .db import Database
from .speedtest import OoklaSpeedtestRunner

LOG = logging.getLogger("pisentinel.speedtest")


@contextlib.contextmanager
def worker_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


class SpeedtestWorker:
    def __init__(self, settings: Settings, runner: OoklaSpeedtestRunner | None = None):
        self.settings = settings
        self.db = Database(settings.db_path, settings.failure_threshold, settings.recovery_threshold,
                           settings.retention_days, settings.incident_retention_days)
        self.runner = runner or OoklaSpeedtestRunner(settings.speedtest_executable,
                                                     settings.speedtest_timeout_seconds)
        self.stopped = False

    def cycle(self) -> bool:
        self.db.recover_stale_speedtest_jobs()
        self.db.schedule_speedtest_if_due()
        job = self.db.claim_speedtest_job()
        if job is None:
            return False
        LOG.info("Iniciando teste Ookla %s (%s)", job["id"], job["trigger"])
        result = self.runner.run()
        self.db.complete_speedtest_job(job["id"], result)
        self.db.prune_speedtests()
        if result["success"]:
            LOG.info("Teste %s concluído: %.2f/%.2f Mbps", job["id"], result["download_mbps"], result["upload_mbps"])
        else:
            LOG.warning("Teste %s falhou: %s", job["id"], result["error"])
        return True

    def run(self, once: bool = False) -> int:
        while not self.stopped:
            try:
                ran = self.cycle()
            except Exception:
                LOG.exception("Ciclo do speedtest não concluído")
                if once:
                    return 1
                ran = False
            if once:
                return 0
            deadline = time.monotonic() + (1 if ran else self.settings.speedtest_poll_seconds)
            while not self.stopped and time.monotonic() < deadline:
                time.sleep(min(1, max(.01, deadline - time.monotonic())))
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Executor isolado dos testes de velocidade Ookla.")
    parser.add_argument("--once", action="store_true", help="Processar no máximo um trabalho e sair")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    with worker_lock(Path(str(settings.db_path) + ".speedtest.lock")):
        worker = SpeedtestWorker(settings)
        signal.signal(signal.SIGTERM, lambda *_: setattr(worker, "stopped", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(worker, "stopped", True))
        return worker.run(args.once)


if __name__ == "__main__":
    raise SystemExit(main())
