"""Minimal .env reader (KEY=VALUE lines, # comments). Real environment variables win."""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path | None) -> dict[str, str]:
    """Return file values overlaid with os.environ. path=None or "-" means environment only."""
    values: dict[str, str] = {}
    if path not in (None, "-"):
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"env file not found: {p.resolve()}")
        for lineno, raw in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"{p}:{lineno}: expected KEY=VALUE, got {raw!r}")
            key, _, value = line.partition("=")
            value = value.split(" #", 1)[0].strip().strip('"').strip("'")
            values[key.strip()] = value
    values.update({k: v for k, v in os.environ.items() if k.startswith("DWR_")})
    return values
