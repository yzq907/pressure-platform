"""Source and frozen executable entry point. Load configuration before app imports."""
import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Mysterious single-port server")
    parser.add_argument("--config", help="Path to an external .env file; relative paths use its directory")
    parser.add_argument("--init-ssh-key", metavar="PATH", help="Create an external SSH master key (never overwrite)")
    args = parser.parse_args()
    if args.init_ssh_key:
        from app.core.ssh_credentials import CredentialEncryptionError, initialize_key_file
        try:
            initialize_key_file(args.init_ssh_key)
        except CredentialEncryptionError as exc:
            parser.error(str(exc))
        print("SSH 主密钥已创建（权限 0600）；请单独备份并配置 MYSTERIOUS_SSH_PASSWORD_KEY_FILE")
        return
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
