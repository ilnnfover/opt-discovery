#!/usr/bin/env python3
"""Thin entry point for :mod:`tools.artifact_diff`."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.artifact_diff import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
