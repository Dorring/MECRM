import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

REPO_ROOT = ROOT.parent
CORE_SRC = REPO_ROOT / "core_services" / "src"
if CORE_SRC.is_dir() and str(CORE_SRC) not in sys.path:
    insert_at = 1 if sys.path and sys.path[0] == str(SRC) else 0
    sys.path.insert(insert_at, str(CORE_SRC))
