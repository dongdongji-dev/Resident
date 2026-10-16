#!/usr/bin/env python3
"""start.py — 起 hub 常驻服务（等同 bash start.sh start）。

用法：python3 start/start.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _hook  # noqa: E402

import subprocess  # noqa: E402

if __name__ == "__main__":
    skill = Path(__file__).resolve().parent.parent.parent
    rc = subprocess.call(["bash", str(skill / "start.sh"), "start"])
    sys.exit(rc)
