"""Console entry point. `python -m ultrak` also runs this, so main() must not exit here."""

import sys

from .cli import main


def gui_entry() -> None:
    """Installed `ultrak-gui`: opens the window, keeps the process alive while it runs."""
    from .gui import VIDEO_EXT, launch

    files = [a for a in sys.argv[1:] if a.lower().endswith(VIDEO_EXT)]
    launch(None, files)


if __name__ == "__main__":
    sys.exit(main())
