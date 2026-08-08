from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = str(ROOT / "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
