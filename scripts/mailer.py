#!/usr/bin/env python3
"""Compatibility entrypoint for running from a cloned or downloaded folder."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mailpilot.mailer import main  # noqa: E402


if __name__ == "__main__":
    main()
