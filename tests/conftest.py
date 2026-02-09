from __future__ import annotations

import sys
from pathlib import Path


# Repo is a collection of scripts, not an installed package.
# Make project root importable for pytest.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
