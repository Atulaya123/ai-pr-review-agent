"""Read or update review controls in the configured Redis, without redeploying."""

import argparse
import asyncio
from logging.config import dictConfig

from backend.configuration.runtime_controls import get_runtime_controls, update_runtime_controls
from backend.observability.logging import WORKER_LOG_CONFIG


async def main() -> None:
    dictConfig(WORKER_LOG_CONFIG)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--posting", choices=("enabled", "disabled"))
    parser.add_argument("--force-hitl", choices=("on", "off"))
    parser.add_argument(
        "--disable-specialist", choices=("security", "quality", "tests", "docs", "none")
    )
    args = parser.parse_args()
    changes: dict[str, object] = {}
    if args.posting is not None:
        changes["posting_disabled"] = args.posting == "disabled"
    if args.force_hitl is not None:
        changes["force_hitl"] = args.force_hitl == "on"
    if args.disable_specialist is not None:
        changes["disabled_specialist"] = (
            None if args.disable_specialist == "none" else args.disable_specialist
        )
    controls = await update_runtime_controls(changes) if changes else await get_runtime_controls()
    print(controls.model_dump_json())
    print(f"version={controls.version}")


if __name__ == "__main__":
    asyncio.run(main())
