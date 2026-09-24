from __future__ import annotations

import logging
import time

from flowweave_admin_metrics.sampler import collect_once
from flowweave_admin_metrics.settings import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    settings = Settings()
    while True:
        try:
            collected = collect_once(settings)
            if collected:
                logger.info("admin metrics sample stored")
        except Exception:
            logger.exception("admin metrics collection failed")
        time.sleep(settings.admin_metrics_sample_interval_seconds)


if __name__ == "__main__":
    main()
