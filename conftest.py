"""Root pytest config.

The legacy tests in legacy/app/Tests do `from main import app`, so legacy/app/ must be
importable.
It is appended (not prepended) so it can never shadow the V1 packages.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_legacy_app = str(ROOT / "legacy" / "app")
if _legacy_app not in sys.path:
    sys.path.append(_legacy_app)
