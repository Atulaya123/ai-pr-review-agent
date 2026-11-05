from typing import Any

from arq.logs import default_log_config


def _worker_log_config() -> dict[str, Any]:
    configuration = default_log_config(verbose=False)
    configuration["loggers"]["backend.memory.tiger_client"] = {
        "handlers": ["arq.standard"],
        "level": "INFO",
        "propagate": False,
    }
    return configuration


WORKER_LOG_CONFIG = _worker_log_config()
