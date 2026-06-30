import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Note: core_services/src is NOT added here because it contains a conflicting
# governance package. Tests that need core_services imports should add the path themselves.

