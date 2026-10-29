"""One-file entry point: `python ultrak.py ...` works straight from a git clone, no install."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from ultrak.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
