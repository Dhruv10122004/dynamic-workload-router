"""Run the controller: python -m controller [--env-file PATH]   (PATH "-" = environment only)

Listens on every address in DWR_BIND_HOSTS (one socket each, one server).
"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path

import psutil
import uvicorn

from controller.main import create_app
from controller.settings import Settings, SettingsError
from shared.envfile import load_env


def _bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    if sys.platform != "win32":  # on Windows SO_REUSEADDR would allow two controllers on one port
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    return sock


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="controller")
    ap.add_argument("--env-file", default=None, help='default: .env if present; "-" = environment only')
    ap.add_argument("--log-file", default=None,
                    help="append stdout/stderr here (start.ps1 uses it so no console handles are inherited)")
    args = ap.parse_args(argv)
    if args.log_file:
        sys.stdout = sys.stderr = open(args.log_file, "a", buffering=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env_file = args.env_file if args.env_file is not None else (".env" if Path(".env").is_file() else "-")
    try:
        settings = Settings.from_env(load_env(env_file))
    except (SettingsError, FileNotFoundError, ValueError) as e:
        print(f"dwr-controller: {e}", file=sys.stderr)
        return 2
    log = logging.getLogger("dwr.controller")
    if settings.controller_cpus:
        try:
            psutil.Process().cpu_affinity(list(settings.controller_cpus))
            log.info("controller pinned to cores %s (away from the Edge slots)", list(settings.controller_cpus))
        except (psutil.Error, OSError, ValueError) as e:
            print(f"dwr-controller: cannot pin to cores {settings.controller_cpus}: {e}", file=sys.stderr)
            return 2
    sockets = []
    for host in settings.bind_hosts:
        try:
            sockets.append(_bind(host, settings.port))
        except OSError as e:
            print(f"dwr-controller: cannot listen on {host}:{settings.port}: {e}\n"
                  f"  Is another controller running? Is {host} an address of this machine?", file=sys.stderr)
            return 2
    log.info(
        "listening on %s (db %s)", ", ".join(f"{h}:{settings.port}" for h in settings.bind_hosts),
        settings.db_path.resolve())
    config = uvicorn.Config(create_app(settings), log_level="info", access_log=False)
    uvicorn.Server(config).run(sockets=sockets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
