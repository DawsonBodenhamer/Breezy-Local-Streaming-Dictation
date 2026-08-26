"""Repository entrypoint for the versioned GitHub release engine."""

import sys
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from release_engine import main


if __name__ == "__main__":
    raise SystemExit(main())
