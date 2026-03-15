"""Entrypoint — run the package cleanup loop."""

from __future__ import annotations

import logging
import signal
import sys
import threading

from assure_package_cleaner.cleaner import Cleaner
from assure_package_cleaner.client import SpectraClient
from assure_package_cleaner.config import Config, ConfigError

logger = logging.getLogger("assure_package_cleaner")

_shutdown = threading.Event()


def _handle_signal(signum, frame):
    logger.info("Received shutdown signal, will exit after current operation")
    _shutdown.set()


def main() -> None:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)  # noqa: T201
        sys.exit(1)

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg.log_settings()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    client = SpectraClient(
        base_url=cfg.base_url,
        org=cfg.org,
        api_token=cfg.api_token,
        request_delay=cfg.request_delay_seconds,
    )
    cleaner = Cleaner(
        client=client,
        stale_threshold_days=cfg.stale_threshold_days,
        dry_run=cfg.dry_run,
    )

    if cfg.cleanup_interval_hours == 0:
        logger.info("Single-run mode (CLEANUP_INTERVAL_HOURS=0)")
        try:
            cleaner.run_cycle()
        except Exception:
            logger.exception("Unexpected error during cleanup cycle")
        return

    interval_seconds = cfg.cleanup_interval_hours * 3600
    logger.info("Periodic mode — running every %d hours", cfg.cleanup_interval_hours)

    while not _shutdown.is_set():
        try:
            cleaner.run_cycle()
        except Exception:
            logger.exception("Unexpected error during cleanup cycle")
        logger.info("Sleeping %d hours until next cycle...", cfg.cleanup_interval_hours)
        _shutdown.wait(interval_seconds)


if __name__ == "__main__":
    main()
