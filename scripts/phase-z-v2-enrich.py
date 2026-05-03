#!/usr/bin/env python3
"""Compatibility wrapper for ``phase-z-v2.py enrich``."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> None:
    target = Path(__file__).with_name("phase-z-v2.py")
    sys.argv = [str(target), "enrich", *sys.argv[1:]]
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
