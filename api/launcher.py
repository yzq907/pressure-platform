"""Source and frozen executable entry point. Load configuration before app imports."""
import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description="Mysterious single-port server")
    parser.add_argument("--config", help="Path to an external .env file; relative paths use its directory")
    args = parser.parse_args()
    if args.config:
        config = Path(args.config).resolve()
        if not config.is_file():
            parser.error(f"Configuration file not found: {config}")
        os.environ["MYSTERIOUS_CONFIG_FILE"] = str(config)
        os.chdir(config.parent)
    from app.core.config import get_settings
    settings = get_settings()
    if getattr(sys, "frozen", False) and not settings.frontend_dist:
        settings.frontend_dist = str(Path(sys.executable).resolve().parent / "dist")
    import uvicorn
    from app.main import app
    uvicorn.run(app, host=settings.server_host, port=settings.server_port, workers=1)


if __name__ == "__main__":
    main()
