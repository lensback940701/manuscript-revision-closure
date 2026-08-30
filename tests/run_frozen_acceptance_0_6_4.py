"""Versioned entrypoint for the MRC 0.6.4 frozen-EXE acceptance."""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    implementation = Path(__file__).with_name("run_frozen_acceptance_0_6_3.py")
    namespace = runpy.run_path(str(implementation))
    namespace["main"]()
