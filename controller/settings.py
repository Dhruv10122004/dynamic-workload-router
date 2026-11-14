"""Controller configuration from .env (DECISIONS D-004). Fails fast with every problem listed."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import psutil


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
    controller_cpus: tuple[int, ...] | None = None

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

        controller_cpus = _controller_cpus(env, errors)
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
            controller_cpus=controller_cpus,
        )


def _controller_cpus(env: Mapping[str, str], errors: list[str]) -> tuple[int, ...] | None:
    """Cores the controller process is pinned to (plan §3: away from the Edge slot cores).

    DWR_CONTROLLER_CPUS wins. Otherwise, when the Edge agent's DWR_SLOT_CPUS is in the
    same .env, use every other core. None = no pinning."""
    ncpu = psutil.cpu_count() or 1
    raw = env.get("DWR_CONTROLLER_CPUS", "").strip()
    if raw:
        try:
            cpus = tuple(sorted({int(c) for c in raw.split(",") if c.strip()}))
        except ValueError:
            errors.append(f"DWR_CONTROLLER_CPUS must be a comma list of cores, got {raw!r}")
            return None
        bad = [c for c in cpus if not 0 <= c < ncpu]
        if bad or not cpus:
            errors.append(f"DWR_CONTROLLER_CPUS: invalid cores {bad or raw!r} (host has 0..{ncpu - 1})")
            return None
        return cpus
    slots = env.get("DWR_SLOT_CPUS", "").strip()
    if not slots:
        return None
    try:
        used = {int(c) for part in slots.split(";") for c in part.split(",") if c.strip()}
    except ValueError:
        return None  # the agent reports a malformed DWR_SLOT_CPUS itself
    rest = tuple(c for c in range(ncpu) if c not in used)
    if not rest:
        errors.append("DWR_SLOT_CPUS uses every core; set DWR_CONTROLLER_CPUS or free a core for the controller")
        return None
    return rest
