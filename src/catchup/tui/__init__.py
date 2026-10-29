"""The terminal UI, through which you read items and act on them."""

import sys

from catchup import CatchupError
from catchup.config import load_config
from catchup.store import Store
from catchup.tui.app import CatchupApp


def run() -> int:
    """Start the TUI and return the exit code."""
    try:
        config = load_config()
        # Open the database once up front, so a broken one fails here with a clear message.
        Store().close()
    except CatchupError as e:
        print(f"catchup: error: {e}", file=sys.stderr)
        return 1
    CatchupApp(config).run()
    return 0
