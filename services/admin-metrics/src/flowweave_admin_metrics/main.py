from __future__ import annotations

import logging
import time

from flowweave_admin_metrics.sampler import cleanup_expired_temporary_services, collect_once
from flowweave_admin_metrics.settings import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    settings = Settings()
    next_temporary_service_cleanup = 0.0
    while True:
        try:
            collected = collect_once(settings)
            if collected:
                logger.info("admin metrics sample stored")
        except Exception:
            logger.exception("admin metrics collection failed")
        if time.monotonic() >= next_temporary_service_cleanup:
            try:
                stopped = cleanup_expired_temporary_services(settings)
                if stopped:
                    logger.info("admin temporary services stopped count=%s", stopped)
            except Exception:
                logger.exception("admin temporary service cleanup failed")
            next_temporary_service_cleanup = (
                time.monotonic() + settings.admin_temporary_service_cleanup_interval_seconds
            )
        time.sleep(settings.admin_metrics_sample_interval_seconds)


if __name__ == "__main__":
    main()
