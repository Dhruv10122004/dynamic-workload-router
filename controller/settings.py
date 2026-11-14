"""Controller configuration from .env (DECISIONS D-004). Fails fast with every problem listed."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    worker_tokens: dict[str, str]
    bind_hosts: tuple[str, ...] = ("127.0.0.1",)
    port: int = 8000
    data_dir: Path = Path("./var/controller")
    admin_token: str | None = None
    test_mode: bool = False
    stale_s: float = 5.0
    heartbeat_interval_s: float = 1.0
    scheduler_interval_s: float = 1.0
    cors_origins: tuple[str, ...] = field(default=("http://localhost:5173", "http://127.0.0.1:5173"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dwr.db"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        errors: list[str] = []

        tokens: dict[str, str] = {}
        raw = env.get("DWR_WORKER_TOKENS", "").strip()
        if not raw:
            errors.append("DWR_WORKER_TOKENS is required (worker_id:token,worker_id:token)")
        for pair in filter(None, (p.strip() for p in raw.split(","))):
            wid, sep, tok = pair.partition(":")
            if not sep or not wid.strip() or not tok.strip():
                errors.append(f"DWR_WORKER_TOKENS entry {pair!r} is not worker_id:token")
            elif wid.strip() in tokens:
                errors.append(f"DWR_WORKER_TOKENS lists {wid.strip()!r} twice")
            else:
                tokens[wid.strip()] = tok.strip()

        hosts = tuple(h.strip() for h in env.get("DWR_BIND_HOSTS", "127.0.0.1").split(",") if h.strip())
        for h in hosts:
            try:
                ipaddress.ip_address(h)
            except ValueError:
                errors.append(f"DWR_BIND_HOSTS: {h!r} is not an IP address")

        def number(key: str, default: float, cast=float):
            v = env.get(key, "").strip()
            if not v:
                return default
            try:
                n = cast(v)
            except ValueError:
                errors.append(f"{key} must be a number, got {v!r}")
                return default
            if n <= 0:
                errors.append(f"{key} must be > 0, got {v!r}")
            return n

        port = number("DWR_PORT", 8000, int)
        stale_s = number("DWR_STALE_S", 5.0)
        test_mode = env.get("DWR_TEST_MODE", "0").strip() in ("1", "true", "yes")
        if errors:
            raise SettingsError("invalid controller configuration:\n  - " + "\n  - ".join(errors))
        return cls(
            worker_tokens=tokens,
            bind_hosts=hosts or ("127.0.0.1",),
            port=port,
            data_dir=Path(env.get("DWR_CONTROLLER_DATA_DIR", "./var/controller")),
            admin_token=env.get("DWR_ADMIN_TOKEN", "").strip() or None,
            test_mode=test_mode,
            stale_s=stale_s,
        )
