"""python -m webapp  ->  http://127.0.0.1:8500/store and /control"""
import logging
import os
import sys
from pathlib import Path

import uvicorn

from storefront.backend import Registry, load_clouds

from .server import create_app

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = Path(os.environ.get("STOREFRONT_CONFIG", ROOT / "results" / "clouds.local.json"))
    try:
        clouds = load_clouds(config)
    except (OSError, ValueError) as exc:
        sys.exit(f"Cannot load cloud config {config}: {exc}")
    port = int(os.environ.get("WEBAPP_PORT", "8500"))
    print(f"\n  Store:        http://127.0.0.1:{port}/store\n  Control room: http://127.0.0.1:{port}/control\n")
    uvicorn.run(create_app(Registry(clouds, home="gcp"), start_monitor=True),
                host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
