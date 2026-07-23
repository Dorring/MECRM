import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Compatibility shim: StrEnum was added in Python 3.11.  CI runs on
# 3.11+, but local dev may use 3.10.  This shim injects StrEnum into
# the enum module so `from enum import StrEnum` works on 3.10.
# On 3.11+ this is a no-op (enum.StrEnum already exists).
if sys.version_info < (3, 11):
    try:
        from enum import StrEnum  # noqa: F401
    except ImportError:
        from backports.strenum import StrEnum as _BackportStrEnum
        import enum
        enum.StrEnum = _BackportStrEnum  # type: ignore[attr-defined]

# Note: core_services/src is NOT added here because it contains a conflicting
# governance package. Tests that need core_services imports should add the path themselves.

