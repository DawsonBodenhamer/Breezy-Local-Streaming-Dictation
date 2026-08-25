"""Windowless entry point for the pinned dictation client."""

from __future__ import annotations

import sys
from pathlib import Path


runtime = Path(__file__).resolve().parent
logs = runtime / "logs"
logs.mkdir(parents=True, exist_ok=True)
sys.stdout = (logs / "production_client.stdout.log").open(
    "a",
    encoding="utf-8",
    buffering=1,
)
sys.stderr = (logs / "production_client.stderr.log").open(
    "a",
    encoding="utf-8",
    buffering=1,
)

from whisper_dictation.cli import main

main()
